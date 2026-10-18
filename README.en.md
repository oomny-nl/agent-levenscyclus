# Identity for AI agents: lifecycle, ownership and recertification

[Nederlands](README.md)

An AI agent is an identity with an owner and an expiry date. This repository shows that an agent
can get the same lifecycle care as an employee: it is provisioned with a human owner, a purpose, an
expiry date and a risk class. It exists only as long as its owner confirms it and follows its
owner when they change jobs. When the owner leaves, the agent is deactivated and loses its access;
its registration and audit trail are kept. Everything it does and everything that happens to it
is recorded in one verifiable log.

The scenario takes place in the fictional municipality of Lindevoort. All names, case files and
content are made up. The code, logs and identifiers are in Dutch.

![Demo recording: joiner, prompt injection refused, mover, leaver and expired agent, with the chained log as evidence](docs/beeld/demo.gif)

The recording shows `make speel` on a running stack (`make demo` runs `make up` and `make speel` in one go): five scenarios against the real stack, followed by the log as evidence. Record it again with `make opname` (requires [VHS](https://github.com/charmbracelet/vhs) and ffmpeg).

## What this proves

- **Ownership is enforceable.** An agent always has a human owner (never another agent). Without
  that owner's confirmation the agent does nothing, not even right after the administrator created
  it.
- **Access follows the owner.** The agent inherits its access from its owner's teams
  (agent → owner → team → case file in OpenFGA). When the owner changes teams (mover), they must
  confirm the agent again and the agent loses the old team. When the owner leaves (leaver), the
  agent is deactivated immediately, even in the middle of a task.
- **Time is a hard boundary.** After the expiry date or a missed recertification the gateway
  refuses the agent, even before a review run. After the expiry date OpenFGA also refuses on its
  own, independent of the gateway.
- **The boundary lies outside the model.** The agent may be hijacked. A prompt injection in a case
  file attachment sends it to a case file of another team; that fails on the relationship, not on
  the model's judgement.
- **One verifiable log.** Lifecycle events and tool calls share one hash chain with an anchor on a
  separate volume. A changed record or a truncated tail shows up.

## Architecture

```mermaid
flowchart LR
    HR["HR / IdP<br/>SCIM client (source)"] -->|Users, Groups| R
    FB["Application administrator<br/>CLI"] -->|agents, review run, overview| R
    E["Owner<br/>Sanne, Kees"] -->|confirmation| R
    R["register<br/>SCIM 2.0 + lifecycle engine + log"]
    P["poort (gateway)<br/>MCP gateway + case file API"]
    A["Agent<br/>LangGraph + MCP client<br/>own key"] -->|"tools/call + agent assertion"| P
    P -->|GET /scim/v2/Users/id| R
    P -->|POST /logboek| R
    subgraph intern["network intern"]
        F[("OpenFGA")]
    end
    R -->|relationship tuples, preshared key| F
    P -->|check, preshared key| F
    R -.->|anchor file| K[("separate volume")]
```

| Component | Role |
|---|---|
| `register` | SCIM 2.0 server (RFC 7643/7644) for employees, teams and agents, with an agent schema extension; lifecycle engine; admin API; chained log; owner of the OpenFGA store and model |
| `openfga` | Relationship-based authorization (`openfga/openfga:v1.22.0`, in-memory, preshared key) |
| `poort` (gateway) | MCP gateway in front of the case file API. Five checks per tool call: assertion, lifecycle, tool, relationship, log. It runs the tool only after the log record is written |
| agent | LangGraph agent with its own ES256 key; identifies itself on every request with a single-use assertion |
| `levenscyclus-beheer` | CLI for the application administrator |

OpenFGA sits only on the `intern` network; the agent sits only on `front`. So it has no route to
OpenFGA, and OpenFGA also requires a preshared key that only the register and the gateway hold.

## Running it

Requirements: Docker with Compose, and `make`.

```bash
make demo
```

`make demo` creates a `.env` with a fresh OpenFGA key (never in git), starts the stack from scratch
and plays the five scenarios. It then shows the evidence table from the log, the chain and anchor
check, and a tampering test. The command exits with code 0 when every outcome matches the design;
CI runs it on every pull request and every push to `main`.

## The scenarios

1. **Joiner.** The administrator provisions `dossierassistent` with owner Sanne. Until Sanne
   confirms it, every tool call is refused (`herbeoordeling_vereist`, recertification required).
   After confirmation it reads 2026-0142 and adds a draft note, via agent → Sanne →
   werk-en-inkomen → case file.
2. **Prompt injection.** An attachment in 2026-0143 tells the agent to read case file 2026-0301 of
   team zorg and add a note there. The hijacked agent tries; both calls fail with
   `relatie_ontbreekt` (no relationship). The legitimate note on 2026-0143 goes through.
3. **Mover.** HR removes Sanne from werk-en-inkomen and adds her to zorg. The agent is refused
   immediately (`herbeoordeling_vereist`, reason `eigenaar_van_team_gewisseld`); each group change
   gives it a new version. After Sanne confirms again, 2026-0142 is closed and 2026-0301 is open.
   Nobody changed anything per agent.
4. **Leaver, mid-task.** The agent reads a case file; HR sets Sanne to `active=false`; the next tool
   call of the same task is refused (`agent_gedeactiveerd`, `eigenaar_vertrokken`).
5. **Expiry without recertification.** Kees confirms `rapportagebot` and `archiefbot`. The demo
   clock moves forward: `archiefbot` expires after 30 days, `rapportagebot` asks for
   recertification after 90 days and is deactivated after the 14-day confirmation period. Each
   time the gateway refuses before the review run; the review run records the status with the
   moment the period ended.

## Evidence table

From the register's log, as `make demo` shows it (local times; the demo clock jumps ahead):

```text
tijd        agent            eigenaar       gebeurtenis/tool       dossier   uitkomst   reden
17-10 19:11 dossierassistent Sanne de Vries herbeoordeling_vereist           beheerder  wacht_op_eigenaar
17-10 19:11 dossierassistent Sanne de Vries bevestigd                        eigenaar   bevestigd_door_eigenaar
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0142 toegestaan
17-10 19:11 dossierassistent Sanne de Vries notitie_toevoegen      2026-0142 toegestaan
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0143 toegestaan
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0301 geweigerd  relatie_ontbreekt
17-10 19:11 dossierassistent Sanne de Vries notitie_toevoegen      2026-0301 geweigerd  relatie_ontbreekt
17-10 19:11 dossierassistent Sanne de Vries notitie_toevoegen      2026-0143 toegestaan
17-10 19:11 dossierassistent Sanne de Vries herbeoordeling_vereist           bron       eigenaar_van_team_gewisseld
17-10 19:11 dossierassistent Sanne de Vries herbeoordeling_vereist           bron       eigenaar_van_team_gewisseld
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0142 geweigerd  herbeoordeling_vereist
17-10 19:11 dossierassistent Sanne de Vries bevestigd                        eigenaar   bevestigd_door_eigenaar
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0142 geweigerd  relatie_ontbreekt
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0301 toegestaan
17-10 19:11 dossierassistent Sanne de Vries dossier_lezen          2026-0301 toegestaan
17-10 19:11 dossierassistent Sanne de Vries gedeactiveerd                    bron       eigenaar_vertrokken
17-10 19:11 dossierassistent Sanne de Vries notitie_toevoegen      2026-0301 geweigerd  agent_gedeactiveerd
17-10 19:11 rapportagebot    Kees Mulder    herbeoordeling_vereist           beheerder  wacht_op_eigenaar
17-10 19:11 archiefbot       Kees Mulder    herbeoordeling_vereist           beheerder  wacht_op_eigenaar
17-10 19:11 rapportagebot    Kees Mulder    bevestigd                        eigenaar   bevestigd_door_eigenaar
17-10 19:11 archiefbot       Kees Mulder    bevestigd                        eigenaar   bevestigd_door_eigenaar
17-10 19:11 rapportagebot    Kees Mulder    dossier_lezen          2026-0301 toegestaan
17-10 19:11 archiefbot       Kees Mulder    dossier_lezen          2026-0301 toegestaan
17-11 18:11                                 demoklok_vooruit                 beheerder  P31D
17-11 18:11 archiefbot       Kees Mulder    dossier_lezen          2026-0301 geweigerd  agent_verlopen
16-01 18:11                                 demoklok_vooruit                 beheerder  P60D
16-01 18:11 rapportagebot    Kees Mulder    dossier_lezen          2026-0301 geweigerd  herbeoordeling_vereist
31-01 18:11                                 demoklok_vooruit                 beheerder  P15D
31-01 18:11 rapportagebot    Kees Mulder    dossier_lezen          2026-0301 geweigerd  agent_gedeactiveerd
31-01 18:11 rapportagebot    Kees Mulder    gedeactiveerd                    controle   niet_herbeoordeeld
31-01 18:11 archiefbot       Kees Mulder    verlopen                         controle   vervaldatum_bereikt
Keten en anker: intact: 42 records, kop a3a8d3a48f672c7e…, niet verankerd: 0
Manipulatieproef: record 12 (dossier_lezen 2026-0301) in een kopie op 'toegestaan' gezet -> keten gebroken bij record 12
```

## Tests

| Command | What |
|---|---|
| `make lint` | `ruff check`, `ruff format --check`, `ty check` |
| `make test` | unit tests on the host; only OpenFGA and HTTP to the register are replaced |
| `make model` | `fga model test` on `openfga/model.fga.yaml`, and a check that `model.json` matches `model.fga` |
| `make integratie` | integration tests against the real stack in Compose, the network boundary from the agent container, and recovery after an OpenFGA restart |
| `make demo` | the five scenarios with the evidence table |

CI runs the same steps plus a secret scan with gitleaks.

## Design and threat model (Dutch)

- [docs/ONTWERP.md](docs/ONTWERP.md): architecture, SCIM profile, lifecycle engine and time
  rules, OpenFGA model, the gateway's five checks, log and anchor.
- [docs/DREIGINGSMODEL.md](docs/DREIGINGSMODEL.md): for each risk from the OWASP Top 10 for Agentic
  Applications 2026 and the OWASP Non-Human Identities Top 10, the control, the test that proves it
  and what remains open.

## How this differs from agent-mandaat

This is the second proof of concept in a series. The first, agent-mandaat, covers delegated access
per task: an employee gives an agent a short-lived, bound token for a single case file.

| | agent-mandaat | agent-levenscyclus |
|---|---|---|
| Question | May this agent, now, on behalf of this employee, do this task? | Does this agent still exist, who owns it, and what may it do through that relationship? |
| Identity | Keycloak client with a key | SCIM 2.0 register with an agent extension; the key is in the SCIM record |
| Permissions | OAuth 2.0 Token Exchange, scope per tool, mandate register | Relationships in OpenFGA plus the lifecycle status |
| Revocation | Revoke the mandate, kill switch on the client | Leaver, mover, expiry and missed recertification take effect automatically |
| Human role | Employee consents per session | Owner confirms periodically and at every change |

The two fit together: provision the agent as here, and grant it access per task as in
agent-mandaat.

## What this does not solve

The register does not replace an IdP, the agent's key does not rotate, OpenFGA runs in memory and
the log is tamper-evident, not tamper-proof. Within its relationships a hijacked agent can still
write nonsense. The full list is in [ONTWERP §7](docs/ONTWERP.md#7-wat-dit-niet-oplost).

## Security and license

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

License: [EUPL-1.2](LICENSE).
