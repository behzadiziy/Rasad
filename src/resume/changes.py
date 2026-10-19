"""Compute exactly what a tailoring plan changes relative to the master resume.

The change set is derived by code from the plan and the master resume; the LLM
never reports its own changes. It drives the highlighted review copy (Task 9)
and the change summary sent to the user (Task 10).

Texts are compared after collapsing whitespace only, so any other difference
(wording, punctuation, letter case) counts as a change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models import Language
from src.resume.models import Bullet, MasterResume, PlanBullet, TailoringPlan

Section = Literal["experiences", "projects", "skills"]


class _ChangeModel(BaseModel):
    """Base for change models: they hold resume content, so validation errors never echo input."""

    model_config = ConfigDict(hide_input_in_errors=True)


class SummaryChange(_ChangeModel):
    """The summary in the master resume (target language) and in the plan."""

    before: str
    after: str
    changed: bool


class BulletChange(_ChangeModel):
    """A bullet whose text differs from the master text in the target language."""

    parent_id: str
    bullet_id: str
    before: str
    after: str


class OmittedItem(_ChangeModel):
    """A master resume item that the plan leaves out, labeled in the target language."""

    id: str
    label: str
    parent_id: str | None = None


class ChangeSet(_ChangeModel):
    """Every difference between a tailored resume and the master resume."""

    language: Language
    summary: SummaryChange
    rephrased_bullets: list[BulletChange] = Field(default_factory=list)
    omitted_experiences: list[OmittedItem] = Field(default_factory=list)
    omitted_bullets: list[OmittedItem] = Field(default_factory=list)
    omitted_projects: list[OmittedItem] = Field(default_factory=list)
    omitted_skills: list[OmittedItem] = Field(default_factory=list)
    reordered_sections: list[Section] = Field(default_factory=list)
    reordered_bullets: list[str] = Field(
        default_factory=list, description="IDs of experiences and projects whose bullets are shown in a new order."
    )
    has_cover_letter: bool = False

    @property
    def omitted_count(self) -> int:
        """Return the number of omitted experiences, bullets, projects, and skills."""
        return (
            len(self.omitted_experiences)
            + len(self.omitted_bullets)
            + len(self.omitted_projects)
            + len(self.omitted_skills)
        )

    @property
    def rephrased_count(self) -> int:
        """Return the number of rephrased bullets, plus one if the summary changed."""
        return len(self.rephrased_bullets) + (1 if self.summary.changed else 0)


def _same_text(a: str, b: str) -> bool:
    """Return True if two texts are equal after collapsing whitespace."""
    return " ".join(a.split()) == " ".join(b.split())


def _order_changed(selected: Sequence[str], master_order: Sequence[str]) -> bool:
    """Return True if the selected IDs that exist in the master are not in their master order."""
    position = {item_id: index for index, item_id in enumerate(master_order)}
    known = [item_id for item_id in selected if item_id in position]
    return known != sorted(known, key=position.__getitem__)


def _unique(ids: Sequence[str]) -> list[str]:
    """Return the IDs without repeats, keeping the first occurrence."""
    return list(dict.fromkeys(ids))


def _compare_bullets(
    parent_id: str,
    master_bullets: Sequence[Bullet],
    plan_bullets: Sequence[PlanBullet],
    language: Language,
    changes: ChangeSet,
) -> None:
    """Record the rephrased, omitted, and reordered bullets of one included experience or project."""
    by_id = {bullet.id: bullet for bullet in master_bullets}
    selected: list[str] = []
    for plan_bullet in plan_bullets:
        master_bullet = by_id.get(plan_bullet.bullet_id)
        if master_bullet is None or plan_bullet.bullet_id in selected:
            continue
        selected.append(plan_bullet.bullet_id)
        before = master_bullet.text.text_for(language)
        if not _same_text(before, plan_bullet.text):
            changes.rephrased_bullets.append(
                BulletChange(parent_id=parent_id, bullet_id=master_bullet.id, before=before, after=plan_bullet.text)
            )
    for bullet in master_bullets:
        if bullet.id not in selected:
            changes.omitted_bullets.append(
                OmittedItem(id=bullet.id, label=bullet.text.text_for(language), parent_id=parent_id)
            )
    if _order_changed(selected, [bullet.id for bullet in master_bullets]):
        changes.reordered_bullets.append(parent_id)


def compute_changes(plan: TailoringPlan, master: MasterResume) -> ChangeSet:
    """Compare a plan with the master resume, in the plan's language.

    Unknown or misplaced IDs (which `validate_plan` reports as errors) are
    ignored, and a repeated ID counts once.
    """
    language = plan.language
    summary_before = master.summary.text_for(language)
    changes = ChangeSet(
        language=language,
        summary=SummaryChange(
            before=summary_before,
            after=plan.summary,
            changed=not _same_text(summary_before, plan.summary),
        ),
        has_cover_letter=bool(plan.cover_letter and plan.cover_letter.strip()),
    )

    experiences = master.experiences_by_id()
    included_experiences: list[str] = []
    for plan_experience in plan.experiences:
        experience = experiences.get(plan_experience.experience_id)
        if experience is None or experience.id in included_experiences:
            continue
        included_experiences.append(experience.id)
        _compare_bullets(experience.id, experience.bullets, plan_experience.bullets, language, changes)
    for experience in master.experiences:
        if experience.id not in included_experiences:
            label = f"{experience.title.text_for(language)} — {experience.company.text_for(language)}"
            changes.omitted_experiences.append(OmittedItem(id=experience.id, label=label))

    projects = master.projects_by_id()
    included_projects: list[str] = []
    for plan_project in plan.projects:
        project = projects.get(plan_project.project_id)
        if project is None or project.id in included_projects:
            continue
        included_projects.append(project.id)
        _compare_bullets(project.id, project.bullets, plan_project.bullets, language, changes)
    for project in master.projects:
        if project.id not in included_projects:
            changes.omitted_projects.append(OmittedItem(id=project.id, label=project.name.text_for(language)))

    skills = master.skills_by_id()
    included_skills = [skill_id for skill_id in _unique(plan.skill_ids) if skill_id in skills]
    for skill in master.skills:
        if skill.id not in included_skills:
            changes.omitted_skills.append(OmittedItem(id=skill.id, label=skill.name.text_for(language)))

    for section, selected, master_order in (
        ("experiences", included_experiences, list(experiences)),
        ("projects", included_projects, list(projects)),
        ("skills", included_skills, list(skills)),
    ):
        if _order_changed(selected, master_order):
            changes.reordered_sections.append(section)
    return changes
