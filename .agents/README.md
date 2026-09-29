# .agents

Instructions for AI coding agents working in this repository. Entry point: [AGENTS.md](../AGENTS.md).

- `rules/`: topic rules agents follow while changing code (`backend.md`).
- `skills/`: task playbooks, one folder per skill (`code-review/`).

To add a skill, create `skills/<name>/SKILL.md` that starts with YAML frontmatter holding
`name` (equal to the folder name) and `description` (one line: when to use it), then the
steps. Every rule must cite the repository document it comes from.
