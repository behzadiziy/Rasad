"""The tailoring engine: ask the LLM for a `TailoringPlan`, validate it against the master resume, compute the changes.

Flow of `tailor_resume`:
1. Detect the job's language and render the prompts (`prompts/*.md`, Jinja2).
2. Call the LLM and validate the plan (`validate_plan`).
3. If there are errors or warnings, retry once with the problems listed in the prompt.
4. If errors remain, raise `TailoringError`; remaining warnings go into the result.

Validation is the code-side "no fabrication" guard: errors for unknown or
misplaced IDs, duplicates, numbers that are not in the master resume, an empty
summary, and the wrong language; warnings for technical terms that the master
resume never mentions.

CLI (calls the LLM API; never changes the job's status):

    uv run python -m src.resume.tailor --job <platform>:<external_id> [--save]

Prompts, plans, and resume text are never logged; log lines carry only IDs,
counts, and token usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from src.database import Database
from src.llm.base import LLMClient, LLMError, LLMUsage
from src.llm.factory import create_llm_client
from src.models import AppConfig, JobPost, Language, Platform, Settings, TailoredResumeRecord, load_app_config
from src.resume.changes import ChangeSet, compute_changes
from src.resume.language import detect_language
from src.resume.models import MAX_BULLETS_PER_ITEM, MasterResume, TailoringPlan, load_master_resume
from src.utils import normalize_text

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_SYSTEM_TEMPLATE = "tailor_system.md"
_USER_TEMPLATE = "tailor_user.md"
_CONFIG_PATH = Path("config/config.yaml")

_MAX_DESCRIPTION_CHARS = 6000
_MAX_BULLET_WORDS = 25
_COVER_LETTER_MIN_WORDS = 120
_COVER_LETTER_MAX_WORDS = 180
_LANGUAGE_NAMES: dict[Language, str] = {"fa": "Persian (Farsi)", "en": "English"}

# ── Numbers ──
# Thousands separators (ASCII comma or Arabic U+066C) between digit groups are
# dropped, so "1,000" and "1000" are the same number. The Arabic decimal
# separator U+066B splits digit runs like ".". Each run of digits is one
# number, compared without leading zeros.
_THOUSANDS_RE = re.compile(r"(?<=\d)[,\u066c](?=\d{3}(?!\d))")
_DIGITS_RE = re.compile(r"\d+")

# ── Output language ──
# Share of Persian letters that separates Persian from English output. It is
# lower than the job-posting threshold because Persian resume text keeps tool
# names in Latin script, while English output has almost no Persian letters.
_OUTPUT_PERSIAN_RATIO_THRESHOLD = 0.2

# ── Technical terms ──
# A Latin token: letters, digits and + # . / - _ inside, without trailing
# punctuation, so "C++", "C#", "Node.js", and "CI/CD" are single tokens.
# Apostrophes split tokens, so "I'm" and "company's" never look like terms.
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./_\-]*")
_TRAILING_PUNCT = ".-/_"
_TERM_PART_SPLIT_RE = re.compile(r"[./_\-]")
_SPECIAL_CHARS = frozenset("+#./")
# The prefix of a token that starts a sentence: start of text, sentence-ending
# punctuation, or a new line (optionally with a list marker).
_SENTENCE_START_RE = re.compile(r"(?:\A\s*|[.!?:][\"')\]]?\s+|\n[\s\-•*]*)\Z")
# Capitalized English words that are not technical terms (salutations, the
# pronoun "I", calendar names); they never trigger a term warning.
_COMMON_CAPITALIZED = frozenset(
    {
        "i", "dear", "hiring", "manager", "team", "sincerely", "regards", "best", "kind", "yours",
        "thank", "thanks", "respectfully", "mr", "ms", "mrs", "dr", "sir", "madam",
        "january", "february", "march", "april", "may", "june", "july", "august", "september",
        "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    }
)  # fmt: skip
# Latin abbreviations that are ordinary prose, not technical terms.
_PROSE_ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs"})


class PlanValidation(BaseModel):
    """The problems found in a plan: errors must be fixed; warnings are shown to the user."""

    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TailoringError(Exception):
    """The plan still had errors after the retry.

    The message holds only the count; `errors` lists the problems (they may
    quote short pieces of resume text, so callers must not log them).
    """

    def __init__(self, message: str, errors: Sequence[str] = ()) -> None:
        """Store the message and the remaining validation errors."""
        super().__init__(message)
        self.errors = list(errors)


class TailoringResult(BaseModel):
    """A validated plan, its computed changes, its remaining warnings, and the LLM usage of all attempts."""

    plan: TailoringPlan
    changes: ChangeSet
    warnings: list[str]
    language: Language
    model: str
    usage: LLMUsage


# ── Validation ─────────────────────────────────────────────────────────────


def _numbers(text: str) -> set[str]:
    """Return the numbers in a text (Persian and Arabic digits included), without leading zeros."""
    normalized = _THOUSANDS_RE.sub("", normalize_text(text).replace("\u066b", "."))
    return {run.lstrip("0") or "0" for run in _DIGITS_RE.findall(normalized)}


def _latin_tokens(text: str) -> list[tuple[str, int]]:
    """Return the Latin tokens that contain a letter, with their start offsets."""
    tokens: list[tuple[str, int]] = []
    for match in _LATIN_TOKEN_RE.finditer(text):
        token = match.group().rstrip(_TRAILING_PUNCT)
        if any(char.isalpha() for char in token):
            tokens.append((token, match.start()))
    return tokens


def _vocabulary(texts: Sequence[str]) -> set[str]:
    """Return the casefolded Latin tokens of `texts`, their parts, and their singular forms."""
    vocabulary: set[str] = set()
    for text in texts:
        for token, _ in _latin_tokens(text):
            folded = token.casefold()
            vocabulary.add(folded)
            vocabulary.update(part for part in _TERM_PART_SPLIT_RE.split(folded) if part)
            if folded.endswith("s"):
                vocabulary.add(folded[:-1])
    return vocabulary


def _is_known_term(token: str, vocabulary: set[str]) -> bool:
    """Return True if the token, its singular, or all of its parts appear in the vocabulary."""
    folded = token.casefold()
    if folded in vocabulary or folded in _PROSE_ABBREVIATIONS:
        return True
    if folded.endswith("s") and folded[:-1] in vocabulary:
        return True
    parts = [part for part in _TERM_PART_SPLIT_RE.split(folded) if part]
    return len(parts) > 1 and all(part in vocabulary for part in parts)


def _is_english_term_candidate(token: str, text: str, start: int) -> bool:
    """Return True if an English-text token looks like a technical term.

    Candidates are acronyms (two or more capitals), mixed-case words, tokens
    with digits or `+ # . /`, and capitalized words that do not start a sentence.
    """
    if sum(1 for char in token if char.isupper()) >= 2:
        return True
    if any(char.isupper() for char in token[1:]):
        return True
    if any(char.isdigit() or char in _SPECIAL_CHARS for char in token):
        return True
    if token[0].isupper():
        if token.casefold() in _COMMON_CAPITALIZED:
            return False
        return _SENTENCE_START_RE.search(text[:start]) is None
    return False


def _term_candidates(text: str, language: Language) -> list[str]:
    """Return the tokens of an output text that must appear in the master resume."""
    candidates: list[str] = []
    for token, start in _latin_tokens(text):
        if language == "fa" or _is_english_term_candidate(token, text, start):
            candidates.append(token)
    return candidates


def _output_texts(plan: TailoringPlan) -> list[tuple[str, str]]:
    """Return `(location, text)` for every employer-facing text of the plan except the cover letter."""
    texts = [("the summary", plan.summary)]
    for experience in plan.experiences:
        texts.extend(
            (f"bullet {bullet.bullet_id} of experience {experience.experience_id}", bullet.text)
            for bullet in experience.bullets
        )
    for project in plan.projects:
        texts.extend((f"bullet {bullet.bullet_id} of project {project.project_id}", bullet.text) for bullet in project.bullets)
    return texts


def _check_structure(plan: TailoringPlan, master: MasterResume, errors: list[str]) -> None:
    """Report unknown, misplaced, and duplicate IDs, over-long bullet lists, and empty texts."""
    experiences = master.experiences_by_id()
    projects = master.projects_by_id()
    skills = master.skills_by_id()
    owners = master.bullet_owners()

    seen_bullets: set[str] = set()
    for kind, parents, known in (
        ("experience", [(item.experience_id, item.bullets) for item in plan.experiences], experiences),
        ("project", [(item.project_id, item.bullets) for item in plan.projects], projects),
    ):
        seen_parents: set[str] = set()
        for parent_id, bullets in parents:
            if parent_id not in known:
                errors.append(f"Unknown {kind} ID {parent_id!r}: use only {kind} IDs from the master resume")
            elif parent_id in seen_parents:
                errors.append(f"Duplicate {kind} ID {parent_id!r}: list each {kind} once")
            seen_parents.add(parent_id)
            if len(bullets) > MAX_BULLETS_PER_ITEM:
                errors.append(
                    f"{kind.capitalize()} {parent_id!r} has {len(bullets)} bullets; the maximum is {MAX_BULLETS_PER_ITEM}"
                )
            for bullet in bullets:
                owner = owners.get(bullet.bullet_id)
                if owner is None:
                    errors.append(f"Unknown bullet ID {bullet.bullet_id!r} under {kind} {parent_id!r}")
                elif owner != parent_id:
                    errors.append(
                        f"Bullet {bullet.bullet_id!r} belongs to {owner!r}, not to {kind} {parent_id!r}: "
                        "use bullets only under their own experience or project"
                    )
                if bullet.bullet_id in seen_bullets:
                    errors.append(f"Duplicate bullet ID {bullet.bullet_id!r}: use each bullet once")
                seen_bullets.add(bullet.bullet_id)
                if not bullet.text.strip():
                    errors.append(f"Bullet {bullet.bullet_id!r} has an empty text")

    seen_skills: set[str] = set()
    for skill_id in plan.skill_ids:
        if skill_id not in skills:
            errors.append(f"Skill ID {skill_id!r} is not in the master resume")
        elif skill_id in seen_skills:
            errors.append(f"Duplicate skill ID {skill_id!r}: list each skill once")
        seen_skills.add(skill_id)

    if not plan.summary.strip():
        errors.append("The summary is empty")


def validate_plan(plan: TailoringPlan, master: MasterResume, job: JobPost, language: Language) -> PlanValidation:
    """Check a plan against the master resume and sort the problems into errors and warnings.

    Errors: unknown IDs, bullets under the wrong experience or project,
    duplicate IDs, skill IDs not in the master resume, more than five bullets
    per item, numbers that do not appear in the master resume, an empty summary
    or bullet, and output (or a declared `language`) that is not the target language.

    Warnings: technical terms that appear nowhere in the master resume. In
    Persian output these are all Latin tokens; in English output, acronyms,
    mixed-case words, tokens with digits or `+ # . /`, and capitalized words
    that do not start a sentence. The cover letter may also use the job's
    company name and title, including their numbers.

    Args:
        plan: The LLM's plan.
        master: The master resume.
        job: The job the plan is for.
        language: The target language (detected from the job).
    """
    errors: list[str] = []
    warnings: list[str] = []
    _check_structure(plan, master, errors)

    master_texts = [*master.localized_texts(), *sorted(master.date_years())]
    master_numbers = {number for text in master_texts for number in _numbers(text)}
    master_vocabulary = _vocabulary(master_texts)
    job_texts = [job.title, job.company]
    cover_numbers = master_numbers | {number for text in job_texts for number in _numbers(text)}
    cover_vocabulary = master_vocabulary | _vocabulary(job_texts)

    checked: list[tuple[str, str, set[str], set[str]]] = [
        (where, text, master_numbers, master_vocabulary) for where, text in _output_texts(plan)
    ]
    if plan.cover_letter:
        checked.append(("the cover letter", plan.cover_letter, cover_numbers, cover_vocabulary))

    for where, text, allowed_numbers, vocabulary in checked:
        for number in sorted(_numbers(text) - allowed_numbers, key=lambda value: (len(value), value)):
            errors.append(f"Number {number} in {where} does not appear in the master resume")
        reported: set[str] = set()
        for term in _term_candidates(text, language):
            folded = term.casefold()
            if folded in reported or _is_known_term(term, vocabulary):
                continue
            reported.add(folded)
            warnings.append(f"Term {term!r} in {where} does not appear in the master resume")

    if plan.language != language:
        errors.append(f"The plan's language is {plan.language!r}, but the target language is {language!r}")
    output = "\n".join(text for _, text, _, _ in checked)
    if output.strip():
        detected = detect_language(output, persian_ratio_threshold=_OUTPUT_PERSIAN_RATIO_THRESHOLD)
        if detected != language:
            errors.append(
                f"The text is written in {_LANGUAGE_NAMES[detected]}, but the target language is "
                f"{_LANGUAGE_NAMES[language]}"
            )
    return PlanValidation(errors=errors, warnings=warnings)


# ── Prompts ────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _environment() -> jinja2.Environment:
    """Return the Jinja2 environment for `prompts/` (plain text: no HTML autoescaping)."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(_PROMPTS_DIR),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Return the dict without None values and empty lists."""
    return {key: value for key, value in data.items() if value is not None and value != []}


def _master_for_prompt(master: MasterResume, language: Language) -> dict[str, Any]:
    """Return the master resume as IDs plus target-language text (no contact details or dates)."""

    def text(value: Any) -> str | None:
        """Return a localized text in the target language, or None."""
        return value.text_for(language) if value is not None else None

    def bullets(items: Sequence[Any]) -> list[dict[str, Any]]:
        """Return bullets as `{id, text}` objects."""
        return [{"id": bullet.id, "text": text(bullet.text)} for bullet in items]

    return _drop_empty(
        {
            "name": text(master.contact.name),
            "summary": text(master.summary),
            "experiences": [
                _drop_empty(
                    {
                        "id": item.id,
                        "title": text(item.title),
                        "company": text(item.company),
                        "bullets": bullets(item.bullets),
                    }
                )
                for item in master.experiences
            ],
            "projects": [
                _drop_empty(
                    {
                        "id": item.id,
                        "name": text(item.name),
                        "description": text(item.description),
                        "bullets": bullets(item.bullets),
                    }
                )
                for item in master.projects
            ],
            "skills": [
                _drop_empty({"id": item.id, "name": text(item.name), "category": text(item.category)})
                for item in master.skills
            ],
            "education": [
                _drop_empty(
                    {
                        "id": item.id,
                        "degree": text(item.degree),
                        "field": text(item.field),
                        "institution": text(item.institution),
                    }
                )
                for item in master.education
            ],
            "certifications": [
                _drop_empty({"id": item.id, "name": text(item.name), "issuer": text(item.issuer)})
                for item in master.certifications
            ],
            "languages": [
                _drop_empty({"id": item.id, "name": text(item.name), "level": text(item.level)})
                for item in master.languages
            ],
        }
    )


def _render_prompts(
    job: JobPost,
    master: MasterResume,
    language: Language,
    *,
    cover_letter: bool,
    problems: Sequence[str] = (),
) -> tuple[str, str]:
    """Render the system and user prompts; `problems` adds the retry section to the user prompt."""
    description = (job.raw_description or "").strip() or "(no description)"
    if len(description) > _MAX_DESCRIPTION_CHARS:
        description = description[:_MAX_DESCRIPTION_CHARS].rstrip() + "\n[…truncated]"
    environment = _environment()
    system = environment.get_template(_SYSTEM_TEMPLATE).render(
        language=language,
        language_name=_LANGUAGE_NAMES[language],
        cover_letter=cover_letter,
        max_bullet_words=_MAX_BULLET_WORDS,
        max_bullets=MAX_BULLETS_PER_ITEM,
        cover_letter_min_words=_COVER_LETTER_MIN_WORDS,
        cover_letter_max_words=_COVER_LETTER_MAX_WORDS,
    )
    user = environment.get_template(_USER_TEMPLATE).render(
        language=language,
        language_name=_LANGUAGE_NAMES[language],
        title=job.title,
        company=job.company or "-",
        city=job.city or "-",
        description=description,
        master_json=json.dumps(_master_for_prompt(master, language), ensure_ascii=False, indent=2),
        problems=list(problems),
    )
    return system, user


# ── Tailoring flow ─────────────────────────────────────────────────────────


def _apply_config(plan: TailoringPlan, config: AppConfig) -> TailoringPlan:
    """Drop the cover letter when `tailoring.cover_letter` is off."""
    if not config.tailoring.cover_letter and plan.cover_letter is not None:
        return plan.model_copy(update={"cover_letter": None})
    return plan


async def tailor_resume(job: JobPost, master: MasterResume, llm: LLMClient, config: AppConfig) -> TailoringResult:
    """Tailor the master resume to a job, retrying once if the first plan has problems.

    Raises:
        LLMError: If an LLM call fails.
        TailoringError: If the plan still has errors after the retry.
    """
    job_ref = f"{job.platform}:{job.external_id}"
    language = detect_language(
        f"{job.title}\n{job.raw_description or ''}",
        persian_ratio_threshold=config.tailoring.persian_ratio_threshold,
    )
    cover_letter = config.tailoring.cover_letter
    system, user = _render_prompts(job, master, language, cover_letter=cover_letter)
    logger.info("Tailoring resume for {} (language {}, prompt {} chars)", job_ref, language, len(system) + len(user))

    result = await llm.generate_structured(system=system, user=user, schema=TailoringPlan)
    plan = _apply_config(result.value, config)
    usage, model = result.usage, result.model
    validation = validate_plan(plan, master, job, language)
    logger.info(
        "Plan for {} attempt 1: {} error(s), {} warning(s)", job_ref, len(validation.errors), len(validation.warnings)
    )

    if validation.errors or validation.warnings:
        _, retry_user = _render_prompts(
            job, master, language, cover_letter=cover_letter, problems=[*validation.errors, *validation.warnings]
        )
        result = await llm.generate_structured(system=system, user=retry_user, schema=TailoringPlan)
        plan = _apply_config(result.value, config)
        usage, model = usage.plus(result.usage), result.model
        validation = validate_plan(plan, master, job, language)
        logger.info(
            "Plan for {} attempt 2: {} error(s), {} warning(s)",
            job_ref,
            len(validation.errors),
            len(validation.warnings),
        )

    if validation.errors:
        raise TailoringError(
            f"The tailoring plan for {job_ref} still has {len(validation.errors)} error(s) after a retry",
            validation.errors,
        )
    changes = compute_changes(plan, master)
    logger.info(
        "Tailored resume for {}: {} rephrased, {} omitted, {} warning(s), tokens total={}",
        job_ref,
        changes.rephrased_count,
        changes.omitted_count,
        len(validation.warnings),
        usage.total_tokens,
    )
    return TailoringResult(
        plan=plan, changes=changes, warnings=validation.warnings, language=language, model=model, usage=usage
    )


# ── CLI ────────────────────────────────────────────────────────────────────

_PLATFORM_ALIASES: dict[str, Platform] = {
    "jobvision": "jobvision",
    "jv": "jobvision",
    "jobinja": "jobinja",
    "jb": "jobinja",
}


def _parse_job_ref(value: str) -> tuple[Platform, str]:
    """Parse `<platform>:<external_id>` (platform `jobvision`/`jv` or `jobinja`/`jb`)."""
    platform_text, _, external_id = value.partition(":")
    platform = _PLATFORM_ALIASES.get(platform_text.strip().lower())
    if platform is None or not external_id.strip():
        raise argparse.ArgumentTypeError(
            f"expected <platform>:<external_id> with platform jobvision (jv) or jobinja (jb), got {value!r}"
        )
    return platform, external_id.strip()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m src.resume.tailor",
        description="Tailor the master resume to one stored job (calls the LLM API; never changes the job status).",
    )
    parser.add_argument("--job", required=True, type=_parse_job_ref, help="the job, as <platform>:<external_id>")
    parser.add_argument("--save", action="store_true", help="store the result as a new tailored resume version")
    return parser.parse_args(argv)


def _print_changes(job: JobPost, result: TailoringResult) -> None:
    """Print the change summary, cover letter, notes, warnings, and token usage."""
    changes, plan = result.changes, result.plan
    print(f"Job {job.platform}:{job.external_id}: {job.title} | {job.company or '-'}")
    print(f"Language: {result.language} | model: {result.model}")
    print()
    print(f"Summary: {'rephrased' if changes.summary.changed else 'unchanged'}")
    if changes.summary.changed:
        print(f"  before: {changes.summary.before}")
        print(f"  after:  {changes.summary.after}")
    print(f"\nRephrased bullets ({len(changes.rephrased_bullets)}):")
    for change in changes.rephrased_bullets:
        print(f"  [{change.parent_id} / {change.bullet_id}]")
        print(f"    before: {change.before}")
        print(f"    after:  {change.after}")
    for title, items in (
        ("Omitted experiences", changes.omitted_experiences),
        ("Omitted projects", changes.omitted_projects),
        ("Omitted bullets", changes.omitted_bullets),
        ("Omitted skills", changes.omitted_skills),
    ):
        print(f"\n{title} ({len(items)}):")
        for item in items:
            prefix = f"{item.parent_id} / " if item.parent_id else ""
            print(f"  - [{prefix}{item.id}] {item.label}")
    print(f"\nReordered sections: {', '.join(changes.reordered_sections) or 'none'}")
    print(f"Reordered bullets in: {', '.join(changes.reordered_bullets) or 'none'}")
    print(f"Cover letter: {'yes' if changes.has_cover_letter else 'no'}")
    if plan.cover_letter:
        print(f"\n{plan.cover_letter}")
    print(f"\nRelevance notes (not sent to the employer):\n{plan.relevance_notes}")
    print(f"\nWarnings ({len(result.warnings)}):")
    for warning in result.warnings:
        print(f"  ⚠️ {warning}")
    usage = result.usage
    print(f"\nTokens: prompt {usage.prompt_tokens}, output {usage.output_tokens}, total {usage.total_tokens}")


async def _run(platform: Platform, external_id: str, *, save: bool) -> int:
    """Load everything, tailor the resume, print the result, and optionally save it; return the exit code."""
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        logger.error("Invalid settings in config/.env ({})", fields)
        return 1
    try:
        config = load_app_config(_CONFIG_PATH)
        master = load_master_resume(config.tailoring.master_resume_path)
        llm = create_llm_client(config, settings)
    except (OSError, ValueError) as exc:
        logger.error("{}", exc)
        return 1

    async with Database(settings.DB_PATH) as db:
        await db.init_db()
        job = await db.get_job(platform, external_id)
        if job is None:
            logger.error("Job {}:{} is not in the database", platform, external_id)
            return 1
        version = await db.count_tailored_resumes(platform, external_id) + 1
        if save and version > config.tailoring.max_versions_per_job:
            logger.error(
                "Job {}:{} already has {} tailored version(s), the maximum (tailoring.max_versions_per_job)",
                platform,
                external_id,
                version - 1,
            )
            return 1

        try:
            async with llm:
                result = await tailor_resume(job, master, llm, config)
        except TailoringError as exc:
            print(f"{exc}:", file=sys.stderr)
            for error in exc.errors:
                print(f"  ✗ {error}", file=sys.stderr)
            return 1
        except LLMError as exc:
            logger.error("LLM call failed: {}", exc)
            return 1

        _print_changes(job, result)
        if save:
            record = TailoredResumeRecord(
                platform=platform,
                external_id=external_id,
                version=version,
                language=result.language,
                llm_model=result.model,
                plan_json=result.plan.model_dump_json(),
                changes_json=result.changes.model_dump_json(),
            )
            resume_id = await db.save_tailored_resume(record)
            print(f"\nSaved tailored resume ID {resume_id} (version {version})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = _parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    platform, external_id = args.job
    try:
        return asyncio.run(_run(platform, external_id, save=args.save))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
