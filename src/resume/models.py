"""The bilingual master resume and the LLM's tailoring plan.

`MasterResume` holds every fact the tailoring engine may use. Each text is a
`LocalizedText` with a Persian and/or English value, and every selectable item
(experience, bullet, project, skill, education, certification, language) has a
kebab-case ID that is unique across the whole document. Dates are `YYYY-MM`:
Gregorian in `start`/`end`/`date`, and optionally Jalali in the `*_jalali` fields.

`TailoringPlan` is the structured answer the LLM must return. It only refers to
master resume items by ID; their facts (employers, titles, dates) always come
from the master resume.

Validation errors never echo input values (`hide_input_in_errors`), because
the input is personal data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import yaml
from loguru import logger
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.models import Language

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_GREGORIAN_YEARS = range(1900, 2101)
_JALALI_YEARS = range(1280, 1481)


def _check_id(value: str) -> str:
    """Require a kebab-case ID such as `acme-backend-2021`."""
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"ID {value!r} must be kebab-case: lowercase letters and digits separated by single '-'")
    return value


def _year_month_checker(calendar: str, years: range, example: str) -> Any:
    """Return a validator for `YYYY-MM` strings whose year lies in `years`."""

    def check(value: str) -> str:
        """Require `YYYY-MM` with a plausible year for the calendar."""
        match = _YEAR_MONTH_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"{calendar} date must be YYYY-MM, e.g. {example!r}")
        if int(match.group(1)) not in years:
            raise ValueError(f"{calendar} year must be between {years.start} and {years.stop - 1}")
        return value

    return check


ItemId = Annotated[str, AfterValidator(_check_id)]
GregorianMonth = Annotated[str, AfterValidator(_year_month_checker("Gregorian", _GREGORIAN_YEARS, "2021-03"))]
JalaliMonth = Annotated[str, AfterValidator(_year_month_checker("Jalali", _JALALI_YEARS, "1400-01"))]


def _check_range(start: str | None, end: str | None, label: str) -> None:
    """Require `end` not to be before `start` when both are set (`YYYY-MM` compares as text)."""
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label} end date is before its start date")


class _ResumeModel(BaseModel):
    """Base for resume models: unknown keys are errors, strings are stripped, inputs never echoed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


# ── Master resume ──────────────────────────────────────────────────────────


class LocalizedText(_ResumeModel):
    """A text in Persian (`fa`) and/or English (`en`); at least one must be set.

    In YAML, a plain string (e.g. `PostgreSQL`) sets both languages to the same text.
    """

    fa: str | None = None
    en: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _plain_string_is_both(cls, data: Any) -> Any:
        """Accept a plain string as the same text in both languages."""
        if isinstance(data, str):
            return {"fa": data, "en": data}
        return data

    @field_validator("fa", "en")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        """Treat a blank text as missing."""
        return value or None

    @model_validator(mode="after")
    def _at_least_one(self) -> LocalizedText:
        """Require a Persian or an English text."""
        if self.fa is None and self.en is None:
            raise ValueError("at least one of 'fa' or 'en' must be set")
        return self

    def text_for(self, lang: Language) -> str:
        """Return the text in `lang`, falling back to the other language."""
        if lang == "fa":
            return self.fa if self.fa is not None else self.en or ""
        return self.en if self.en is not None else self.fa or ""

    def values(self) -> list[str]:
        """Return the texts that are set, in both languages."""
        return [text for text in (self.fa, self.en) if text is not None]


class Link(_ResumeModel):
    """A labeled URL, such as a GitHub profile or a project demo."""

    label: str | None = None
    url: str = Field(min_length=1)


class Contact(_ResumeModel):
    """The candidate's name and contact details."""

    name: LocalizedText
    email: str | None = None
    phone: str | None = None
    location: LocalizedText | None = None
    links: list[Link] = Field(default_factory=list)


class Bullet(_ResumeModel):
    """One achievement or responsibility under an experience or a project."""

    id: ItemId
    text: LocalizedText


class Experience(_ResumeModel):
    """A position held; `end=None` means it is the current position."""

    id: ItemId
    company: LocalizedText
    title: LocalizedText
    location: LocalizedText | None = None
    start: GregorianMonth
    end: GregorianMonth | None = None
    start_jalali: JalaliMonth | None = None
    end_jalali: JalaliMonth | None = None
    bullets: list[Bullet] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_consistent(self) -> Experience:
        """Require ordered dates, and no Jalali end date for a current position."""
        _check_range(self.start, self.end, f"Experience {self.id!r}")
        _check_range(self.start_jalali, self.end_jalali, f"Experience {self.id!r} (Jalali)")
        if self.end is None and self.end_jalali is not None:
            raise ValueError(f"Experience {self.id!r} has end_jalali but no end (a current position has neither)")
        return self


class Project(_ResumeModel):
    """A project, with optional bullets and links."""

    id: ItemId
    name: LocalizedText
    description: LocalizedText | None = None
    bullets: list[Bullet] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)


class Skill(_ResumeModel):
    """A skill or tool, named exactly as written (e.g. `PostgreSQL`), with an optional category."""

    id: ItemId
    name: LocalizedText
    category: LocalizedText | None = None


class Education(_ResumeModel):
    """A degree; `end=None` with a `start` means it is in progress."""

    id: ItemId
    institution: LocalizedText
    degree: LocalizedText
    field: LocalizedText | None = None
    start: GregorianMonth | None = None
    end: GregorianMonth | None = None
    start_jalali: JalaliMonth | None = None
    end_jalali: JalaliMonth | None = None

    @model_validator(mode="after")
    def _dates_consistent(self) -> Education:
        """Require ordered dates."""
        _check_range(self.start, self.end, f"Education {self.id!r}")
        _check_range(self.start_jalali, self.end_jalali, f"Education {self.id!r} (Jalali)")
        return self


class Certification(_ResumeModel):
    """A certificate or course completion."""

    id: ItemId
    name: LocalizedText
    issuer: LocalizedText | None = None
    date: GregorianMonth | None = None
    date_jalali: JalaliMonth | None = None
    url: str | None = None


class SpokenLanguage(_ResumeModel):
    """A language the candidate speaks, with an optional level."""

    id: ItemId
    name: LocalizedText
    level: LocalizedText | None = None


class MasterResume(_ResumeModel):
    """The complete bilingual resume: the only source of facts for tailoring."""

    contact: Contact
    summary: LocalizedText
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[SpokenLanguage] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterResume:
        """Require every item ID to be unique across the whole document."""
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item_id in self.all_ids():
            if item_id in seen:
                duplicates.add(item_id)
            seen.add(item_id)
        if duplicates:
            raise ValueError(f"IDs must be unique across the resume; duplicates: {sorted(duplicates)}")
        return self

    def all_ids(self) -> list[str]:
        """Return every item ID (including bullet IDs), in document order."""
        ids: list[str] = []
        for experience in self.experiences:
            ids.append(experience.id)
            ids.extend(bullet.id for bullet in experience.bullets)
        for project in self.projects:
            ids.append(project.id)
            ids.extend(bullet.id for bullet in project.bullets)
        for group in (self.skills, self.education, self.certifications, self.languages):
            ids.extend(item.id for item in group)
        return ids

    def experiences_by_id(self) -> dict[str, Experience]:
        """Return the experiences keyed by ID, in document order."""
        return {experience.id: experience for experience in self.experiences}

    def projects_by_id(self) -> dict[str, Project]:
        """Return the projects keyed by ID, in document order."""
        return {project.id: project for project in self.projects}

    def skills_by_id(self) -> dict[str, Skill]:
        """Return the skills keyed by ID, in document order."""
        return {skill.id: skill for skill in self.skills}

    def bullet_owners(self) -> dict[str, str]:
        """Return the ID of the experience or project that owns each bullet ID."""
        owners: dict[str, str] = {}
        for parent in (*self.experiences, *self.projects):
            for bullet in parent.bullets:
                owners[bullet.id] = parent.id
        return owners

    def localized_texts(self) -> Iterator[str]:
        """Yield every localized text in both languages: the candidate's name and
        location and all section content, but not email, phone, URLs, IDs, or dates."""
        yield from _walk_localized(self)

    def date_years(self) -> set[str]:
        """Return the years (Gregorian and Jalali) of every date in the resume."""
        years: set[str] = set()
        dated: list[Experience | Education] = [*self.experiences, *self.education]
        for item in dated:
            for value in (item.start, item.end, item.start_jalali, item.end_jalali):
                if value is not None:
                    years.add(value[:4])
        for certification in self.certifications:
            for value in (certification.date, certification.date_jalali):
                if value is not None:
                    years.add(value[:4])
        return years


def _walk_localized(node: Any) -> Iterator[str]:
    """Yield the values of every `LocalizedText` inside a model, depth first."""
    if isinstance(node, LocalizedText):
        yield from node.values()
    elif isinstance(node, BaseModel):
        for name in type(node).model_fields:
            yield from _walk_localized(getattr(node, name))
    elif isinstance(node, list):
        for item in node:
            yield from _walk_localized(item)


def load_master_resume(path: str | Path) -> MasterResume:
    """Read and validate a master resume YAML file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or fails validation. The message
            names the file and the failing fields, never their values.
    """
    resume_path = Path(path)
    text = resume_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ValueError(f"Invalid master resume '{resume_path}': malformed YAML{where}") from None
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid master resume '{resume_path}': top level must be a mapping, got {type(data).__name__}"
        )
    try:
        resume = MasterResume.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid master resume '{resume_path}':\n{exc}") from None

    logger.info(
        "Loaded master resume from {}: {} experience(s), {} project(s), {} skill(s)",
        resume_path,
        len(resume.experiences),
        len(resume.projects),
        len(resume.skills),
    )
    return resume


# ── Tailoring plan (the LLM output schema) ─────────────────────────────────

MAX_BULLETS_PER_ITEM = 5


class PlanBullet(BaseModel):
    """One selected bullet, possibly rephrased."""

    model_config = ConfigDict(hide_input_in_errors=True)

    bullet_id: str = Field(
        description="ID of a bullet that belongs to this experience or project in the master resume."
    )
    text: str = Field(
        description=(
            "The bullet in the target language, at most 25 words: the master text as it is, or rephrased "
            "to mirror the job's wording when the underlying fact is identical. Never add facts, numbers, "
            "tools, or skills that this master bullet does not contain."
        )
    )


class PlanExperience(BaseModel):
    """An experience to include, with its selected bullets."""

    model_config = ConfigDict(hide_input_in_errors=True)

    experience_id: str = Field(description="ID of an experience in the master resume.")
    bullets: list[PlanBullet] = Field(
        description=(
            f"At most {MAX_BULLETS_PER_ITEM} of this experience's own bullets, most relevant to the job first."
        ),
        json_schema_extra={"maxItems": MAX_BULLETS_PER_ITEM},
    )


class PlanProject(BaseModel):
    """A project to include, with its selected bullets."""

    model_config = ConfigDict(hide_input_in_errors=True)

    project_id: str = Field(description="ID of a project in the master resume.")
    bullets: list[PlanBullet] = Field(
        description=(
            f"At most {MAX_BULLETS_PER_ITEM} of this project's own bullets, most relevant to the job first; "
            "may be empty."
        ),
        json_schema_extra={"maxItems": MAX_BULLETS_PER_ITEM},
    )


class TailoringPlan(BaseModel):
    """The LLM's answer: which master resume items to show, in what order, and how to phrase them.

    Code turns the plan into a resume: titles, employers, dates, education,
    certifications, and languages always come from the master resume.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    language: Language = Field(description="The target language: 'fa' for Persian or 'en' for English.")
    summary: str = Field(
        description=(
            "The professional summary in the target language, 2-4 sentences, rephrased from the master "
            "summary to fit the job. Use only facts stated in the master resume."
        )
    )
    experiences: list[PlanExperience] = Field(
        description=(
            "Experiences to show, in display order (most relevant to the job first). Prefer keeping every "
            "experience; omit one only if it is clearly irrelevant to the job."
        )
    )
    projects: list[PlanProject] = Field(
        description="Projects to show, in display order (most relevant first); omit irrelevant projects."
    )
    skill_ids: list[str] = Field(
        description=(
            "IDs of master resume skills to show, in display order (most relevant first); omit skills that "
            "are irrelevant to the job. Never invent IDs."
        )
    )
    cover_letter: str | None = Field(
        description=(
            "A cover letter in the target language, 120-180 words, addressed to the company, making no claim "
            "that is not in the master resume; null when no cover letter is requested."
        )
    )
    relevance_notes: str = Field(
        description=(
            "Notes for the candidate only (never sent to the employer), in the target language: how well the "
            "resume fits the job, and which job requirements the master resume does not cover."
        )
    )
