# Set up project roles

Use this guide when starting Factory in a project or changing its roles.
The [Factory method](../SKILL.md) owns authority, current state, independence
and handoffs. These templates add duties, not another operating method.

## Find the existing project

Read the applicable repository instructions and project standards first.
Every owner follows them, including the Project manager. Without a repository,
use the project's existing instructions and sources. Keep host rules, required
reviews and reserved human decisions in force.

Find the requested outcome, human owner, existing manager, role roster and
current records. Reuse them. A new request, title or feature does not replace
an existing owner. Do not infer permission to create persistent chats, publish,
spend or change a live system from a role template.

For a new project, use the direct assignment and project instructions to
establish the minimum missing records within authority. A missing document
format is not a blocker when identity and authority are clear. Do not overwrite
another owner's record or make up a chat ID to complete setup.

## Select only useful roles

Start with a Project manager. It can cover several responsibilities and use
temporary helpers. Add a persistent owner when an area needs continuing,
distinct ownership. Reuse required independent reviewers without creating a
permanent QA owner solely for one check.

| Starting role | Responsibility | Often combined with |
| --- | --- | --- |
| [Project manager](roles/project-manager/AGENTS.md) | Priorities, ownership, coordination and the human front door. | Product on small projects. |
| [Product](roles/product/AGENTS.md) | User problems, scope and acceptance criteria. | Project manager or Research and Analysis. |
| [UX/UI](roles/ux-ui/AGENTS.md) | Task flows, interaction, visual design and accessibility. | Product or Content and Documentation. |
| [Engineering](roles/engineering/AGENTS.md) | Implementation, architecture and technical checks. | Data and Integrations or Release and Operations. |
| [QA and Verification](roles/qa-verification/AGENTS.md) | Independent evidence and regression checks. | Keep the verifier independent of the work's author. |
| [Release and Operations](roles/release-operations/AGENTS.md) | Deployment, recovery and operational health. | Engineering. |
| [Data and Integrations](roles/data-integrations/AGENTS.md) | Workflows, migrations, mappings and reconciliation. | Engineering. |
| [Research and Analysis](roles/research-analysis/AGENTS.md) | Traceable evidence, analysis and recommendations. | Product. |
| [Content and Documentation](roles/content-documentation/AGENTS.md) | Documents, content and current project knowledge. | Any owner for its own area. |
| [Domain specialist](roles/domain-specialist/AGENTS.md) | Expertise in a field such as Finance, Security, Legal or People. | Rename and adapt to the field. |

Role names describe responsibilities, not required departments. Route a handoff
to whoever currently owns that responsibility, including a combined owner.
Custom roles are valid. Architecture normally stays with Engineering;
coordination stays with the Project manager. Split either only for a concrete
need. Domain expertise comes from applicable specialist skills, standards and
evidence; a role label supplies neither expertise nor approval authority.

For example, a market study can use one manager covering Product and a temporary
research helper. A continuing integration programme may need a Data owner and
a Finance specialist. Neither example requires the full library.

## Bind an owner to its work

Read only the role templates you select. Adapt their duties to the project,
or write a custom role with the same compact shape. Keep the stable definition
in an existing role record or a separate durable file. A role must outlast a
single feature; a dated feature record can remain evidence without becoming
the role's permanent current-state source.

Use the existing record format. This brief is an example, not a required schema:

```text
Project:              <project name>
Role:                 <assigned responsibility or combined roles>
Role definition:      <stable file or record section>
Project instructions: <applicable instruction sources and standards>
Current state:        <canonical outcome record or existing backlog view>
```

Fill ordinary placeholders by hand when adopting a template. Leave out
irrelevant fields and mark harmless unknowns `not recorded`. Do not leave
identity or authority ambiguous. Fix relative links when moving or embedding
role text. Keep private paths and chat IDs in the project's permitted records,
not in reusable or public templates.

The current-state source may be the roster itself. Keep each outcome's current
assignment, revision, authority and resource claims there once, and link to it
elsewhere. Read the revision at that source; do not pin another changing copy
in the brief. One role can follow several outcomes through the existing backlog.

Reuse the real owner chat and give it the brief and selected role text through
the host's authorised path. Save the returned chat link or ID in the roster
when one exists. If the host cannot create or resume a persistent owner, report
the actual limitation and proposed setup. A saved file or temporary helper
does not prove that a persistent owner was created.

## Make recovery discoverable

Use the project's existing instruction route to give owners their role and
current-state locations. Require a fresh read from the current-state source.
Add or repair that short instruction when editing the source is within scope.
Keep the common method in this skill; do not copy it into every role or replace
global instruction links.

For example, adapt this text in the source the active client loads on resume:

```text
When this role starts or resumes, read its binding at <role-record>.
Read the current revision at <current-state-source> before resumed work,
shared-state writes or subagent dispatch. Follow superseded pointers to the current source.
Earlier reads and compaction summaries are historical evidence.
```

Resolve the locations when adopting this text. Verify that the client actually
loads it after compaction. A route kept only in an opening message or a skill
that is no longer loaded cannot establish recovery.

Give each owner an explicit role binding in its supported persistent
instructions or startup brief. Owners must recover from that binding and
current state, not guess from a title, working directory or their own chat ID.
If the binding is lost, hold affected writes and resolve it through the owner.

These `AGENTS.md` files are source templates. Their folders do not bind a
chat's role, and a Markdown link does not load instructions automatically.
Keep code writers in their authorised worktrees. Use the active client's
supported instruction route; do not assume identical discovery, imports or
compaction behaviour in Codex and Claude Code.

## Verify the setup

Check the actual owner identities, selected duties, instruction routes and
current-state pointers. Complete one bounded useful assignment. Confirm that
the owner reads the current record and acts only within its allocation.
Keep actual installation, fresh discovery, role loading and human acceptance
as separate evidence.

Use the [continuity acceptance check](../../../references/continuity-check.md)
for recovery, including compaction in the clients being claimed. A setup on
one client does not prove another. Reuse passing evidence for unchanged paths;
add a loader or hook only after a demonstrated failure calls for it.
