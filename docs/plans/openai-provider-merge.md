# Plan: OpenAI-Provider verschmelzen (API-Key + Subscription in einem Provider)

**Goal:** Es existiert genau **ein** `openai`-Provider mit zwei Connections — `api-key` (Platform, `/chat/completions`) und `subscription` (ChatGPT-Codex, `/codex/responses`) — bedient von **einem** OpenAI-Adapter, der die Wire-Variante pro Connection wählt. Der separate Provider `openai-subscription` verschwindet vollständig, und der bestehende ChatGPT-Login funktioniert ohne erneutes Einloggen weiter.

**Context:**
Beim Integrieren der ChatGPT-Subscription wurde fälschlich ein **zweiter Provider** (`openai-subscription`, Adapter `openai_subscription`) angelegt, statt dem bestehenden `openai`-Provider eine zweite Connection zu geben. Die Architektur unterstützt Multi-Connection-Provider bereits (der `::connection`-Suffix am Modell, `ConnectionConfig.base_url` pro Connection, ein Adapter pro Provider der pro Connection instanziiert wird). Es fehlten nur zwei Dinge:

- **(A)** Der Adapter erfährt beim Bau nicht, *welche Wire-Variante* die Connection braucht → wir geben ihm ein deklaratives `mode`-Feld.
- **(B)** Die beiden Connections bedienen **unterschiedliche Modell-Sets** (Platform- vs. Codex-Modelle). Bisher galt ein Provider-Katalog implizit für alle Connections → wir binden jedes Modell per `connections`-Allowlist an seine Connection(s).

Beide Designentscheidungen sind vom User bestätigt.

**Wichtige Einschränkung (vom User):** Die **Migration der Nutzerdaten in `~/.vbot`** (OAuth-Token-Datei, Agent-Modell-String) wird **von Hand** gemacht — es darf **kein** Migrations-/Fallback-Code in der App entstehen (PROJECT.md: "No legacy compatibility in app code — ever"). Die App liest nur das neue Format. Der resources-Katalog (`resources/...`) ist hingegen ausgelieferte Quelldatei und wird als Teil der Code-Änderung editiert.

**Scope:**
- **In:** Per-Connection-Felder (`mode`, `models_endpoint`) in `ConnectionConfig`; ein dualer `OpenAIAdapter`; `connection_mode`-Durchreichung in `get_adapter`; Per-Modell-`connections`-Allowlist im Katalog + Filterung in Listing/Targets; connection-bewusste Discovery mit Merge; Merge der Provider-JSONs und Modell-Kataloge; Löschen aller `openai-subscription`-Artefakte; Spec/Docs-Updates; Tests; manuelle `~/.vbot`-Migration.
- **Out:** Kein Migrations-Code in der App. Keine Änderung an anderen Providern außer dem minimal nötigen `connection_mode`-Konstruktor-Argument (akzeptiert + ignoriert). Keine neue OpenAI-Platform-Discovery (`api-key` hat weiterhin keinen `models_endpoint`).

**Assumptions & Constraints:**
- Token-Dateipfad ist `<data_dir>/oauth/<provider_id>-<local_connection_id>.json` (bestätigt in [token_store.py:104](core/providers/token_store.py#L104)). Provider `openai` + Connection `subscription` ⇒ Datei `openai-subscription.json`.
- Codex-Verhalten (Token-Refresh-Extra, Device-Flow-Polling) hängt an `oauth.device_flow == "openai_codex"`, **nicht** an der Provider-ID (bestätigt in [token_getter.py:126](core/providers/token_getter.py#L126), [auth_flow.py:473](core/providers/auth_flow.py#L473)). Umbenennen der Provider-ID bricht das nicht, solange der Connection-`oauth`-Block erhalten bleibt.
- Die WebUI ist datengetrieben (`connection.list`, `model.list`); es gibt **keine** hartcodierte `openai-subscription`-Provider-ID im `webui/src`-Quellcode (nur in Test-Fixtures).
- Der einzige `~/.vbot`-Verweis auf das alte Schema ist `agents/openai/agent.json` (`"openai-subscription/gpt-5.5::oauth"`). `settings.json`-`model_tasks` zeigen alle auf `openrouter`.
- Connection-IDs (lokal): **`api-key`** und **`subscription`**. Der alte Platzhalter-Connector `openai:oauth` (`OPENAI_OAUTH_TOKEN`-Stub) wird durch `subscription` **ersetzt** (entfernt).

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Config-Layer | `ConnectionConfig` trägt `mode` + `models_endpoint`; Parser + Tests grün |
| M2 | Adapter-Layer | `OpenAIAdapter` bedient beide Modi; `get_adapter` reicht `connection_mode` durch; Maps aktualisiert |
| M3 | Katalog-Connection-Awareness | `Model.connections`; Listing/Targets filtern; Payload trägt `connections` |
| M4 | Discovery | Connection-bewusster Refresh mit Merge (andere Connections bleiben erhalten) |
| M5 | Resources & Daten | `openai.json` (Provider+Modelle) gemerged; `openai-subscription`-Artefakte gelöscht |
| M6 | Manuelle Migration | Token-Datei umbenannt, Agent-Modell-String angepasst; Login funktioniert |
| M7 | Specs/Docs | Specs gemerged, Index aktualisiert |

### Phase Breakdown

---

#### Phase 1: Per-Connection-Konfigurationsfelder
**Goal of this phase:** `ConnectionConfig` kann pro Connection eine Wire-Variante (`mode`) und einen Modell-Endpoint (`models_endpoint`) deklarieren. Provider-Level bleibt Fallback.
**Can run in parallel with:** none (Fundament).

- In [providers.py](core/providers/providers.py): `ConnectionConfig` (dataclass) um zwei optionale Felder erweitern:
  - `mode: str | None = None` — frei interpretierter Wire-Variant-Selektor, vom Provider-Adapter ausgewertet.
  - `models_endpoint: str | None = None` — per-Connection-Discovery-Endpoint, überschreibt Provider-Level.
- In `ProviderRegistry._parse_connections`: beide Felder aus `connection_data.get("mode")` / `connection_data.get("models_endpoint")` lesen und in `ConnectionConfig` setzen. `mode`, falls vorhanden, muss `str` sein (sonst `ConfigError`); `models_endpoint` ebenso.
- Tests: in [test_providers.py](tests/core/providers/test_providers.py) Parsing der neuen Felder (gesetzt + weggelassen) abdecken.
- read: `.vorch/specs/providers.md`
- files: [core/providers/providers.py](core/providers/providers.py), [tests/core/providers/test_providers.py](tests/core/providers/test_providers.py)

**Dependencies:** keine
**Done when:** `ProviderConfig` mit einer Connection, die `"mode": "codex_responses"` und `"models_endpoint": "/codex/models"` trägt, parst korrekt; eine Connection ohne diese Felder behält `None`. `python scripts/quality.py core/providers/providers.py` grün.

---

#### Phase 2: Dualer OpenAI-Adapter + `connection_mode`-Plumbing  ⚡ *parallel zu Phase 3*
**Goal of this phase:** Ein einziger `OpenAIAdapter` bedient `chat_completions` (Default/`api-key`) **und** `codex_responses` (`subscription`). Die Codex-Wire-Header werden adapter-eigen (nicht mehr aus Provider-`extra_headers`). `get_adapter` reicht den Connection-`mode` durch.
**Can run in parallel with:** Phase 3 (disjunkte Dateien: Phase 2 = Adapter + `runtime.py` + Discovery-**Map**; Phase 3 = `models.py` + `model_tasks.py` + `payloads.py` + `model.list`).

- **Adapter zusammenführen:** [openai_subscription.py](core/providers/openai_subscription.py) → umbenennen/umwandeln zu `core/providers/openai.py`, Klasse `OpenAISubscriptionAdapter` → **`OpenAIAdapter`** (weiterhin Subklasse von `OpenAICompatibleAdapter`). Begründung: Codex-Logik darf **nicht** in den generischen `OpenAICompatibleAdapter` (von OpenRouter etc. genutzt).
  - Konstante `CODEX_RESPONSES_MODE = "codex_responses"` definieren.
  - Konstante `CODEX_EXTRA_HEADERS = {"OpenAI-Beta": "responses=experimental", "originator": "vbot"}` definieren (wandert aus der Provider-JSON in den Adapter).
  - `send()` / `stream()` / `normalize_response()`: **verzweigen** — wenn `self._connection_mode == CODEX_RESPONSES_MODE` → bestehende Codex-Responses-Logik; sonst `return await super().send(...)` / `super().stream(...)` / `super().normalize_response(...)` (geerbtes `/chat/completions`).
  - `_build_headers()` (Codex-Pfad): zusätzlich `CODEX_EXTRA_HEADERS` mergen (statt auf `self._config.extra_headers` zu vertrauen). Der `chat_completions`-Pfad nutzt das geerbte `_build_headers` von `OpenAICompatibleAdapter`.
  - Klassmethoden `discovery_headers()` / `discovery_params()` / `normalize_catalog_entry()` bleiben Codex-geprägt (nur die `subscription`-Connection hat einen `models_endpoint`, also läuft Discovery nur dafür). `discovery_headers()` zusätzlich `CODEX_EXTRA_HEADERS` einmergen (da der Provider künftig kein `extra_headers` mehr setzt).
  - `OpenAISubscriptionResponsesPolicy` + Helper-Funktionen ziehen mit in `openai.py` um.
- **`openai_subscription_auth.py` bleibt** unverändert (Helper `extract_chatgpt_account_id`, `openai_subscription_token_extra`; weiter von `openai.py`, `token_getter.py`, `auth_flow.py` importiert).
- **`connection_mode` durchreichen:** in [openai_compatible.py](core/providers/openai_compatible.py) `__init__` ein keyword-only `connection_mode: str | None = None` ergänzen und als `self._connection_mode = connection_mode` ablegen. Da `OpenAIAdapter` keinen eigenen `__init__` hat, erbt es das Feld.
  - In [anthropic.py](core/providers/anthropic.py) und [opencode_go.py](core/providers/opencode_go.py) (haben eigene `__init__`) denselben keyword-only Parameter `connection_mode: str | None = None` ergänzen — wird akzeptiert und ignoriert (einheitliche Aufrufstelle).
- **`get_adapter`:** in [runtime.py:927](core/runtime/runtime.py#L927) den Adapter-Aufruf um `connection_mode=connection.mode` erweitern.
- **Adapter-Maps:** in [runtime.py:97-107](core/runtime/runtime.py#L97) `_ADAPTER_MAP`: Eintrag `"openai_subscription": OpenAISubscriptionAdapter` entfernen, `"openai": OpenAIAdapter` ergänzen. Import [runtime.py:34](core/runtime/runtime.py#L34) anpassen. In [discovery.py:428](core/models/discovery.py#L428) `_DISCOVERY_ADAPTER_MAP` analog (`"openai_subscription"` → `"openai"`), Import [discovery.py:24](core/models/discovery.py#L24) anpassen.
- **`__init__`-Export:** [core/providers/__init__.py:17](core/providers/__init__.py#L17) `OpenAISubscriptionAdapter` → `OpenAIAdapter`.
- Tests: [test_openai_subscription.py](tests/core/providers/test_openai_subscription.py) → umbenennen zu `tests/core/providers/test_openai.py` und auf `OpenAIAdapter` umstellen; **beide** Modi testen: `connection_mode="codex_responses"` (Codex-Wire, Account-Header, `store:false`, `instructions`, Reasoning-Mapping) und Default-Modus (delegiert an `/chat/completions`). Map-/`get_adapter`-Tests in [test_runtime_providers.py](tests/core/runtime/test_runtime_providers.py): `get_adapter("openai", "openai:subscription")` liefert `OpenAIAdapter` mit `_connection_mode == "codex_responses"`; `get_adapter("openai", "openai:api-key")` liefert `OpenAIAdapter` mit `_connection_mode is None`.
- read: `.vorch/specs/providers/openai.md`, `.vorch/specs/providers/openai-subscription.md`, `.vorch/specs/runtime.md`
- files: [core/providers/openai.py](core/providers/openai.py) *(neu, aus openai_subscription.py)*, [core/providers/openai_subscription.py](core/providers/openai_subscription.py) *(löschen)*, [core/providers/openai_compatible.py](core/providers/openai_compatible.py), [core/providers/anthropic.py](core/providers/anthropic.py), [core/providers/opencode_go.py](core/providers/opencode_go.py), [core/providers/__init__.py](core/providers/__init__.py), [core/runtime/runtime.py](core/runtime/runtime.py), [core/models/discovery.py](core/models/discovery.py) *(nur Import + `_DISCOVERY_ADAPTER_MAP`)*, [tests/core/providers/test_openai.py](tests/core/providers/test_openai.py) *(neu)*, [tests/core/runtime/test_runtime_providers.py](tests/core/runtime/test_runtime_providers.py)

**Dependencies:** Phase 1 (`ConnectionConfig.mode`).
**Done when:** `get_adapter` liefert für beide Connections denselben `OpenAIAdapter`-Typ mit korrektem `_connection_mode`; Codex-Tests grün; `python scripts/quality.py core/providers/ core/runtime/runtime.py` grün.

> ⚠️ Hinweis an Executor: Phase 2 editiert in `discovery.py` **nur** Import-Zeile + `_DISCOVERY_ADAPTER_MAP`. Die Refresh-Logik (Phase 4) ist eine andere Stelle derselben Datei — Phase 4 läuft danach.

---

#### Phase 3: Modell↔Connection-Bindung (Katalog-Allowlist)  ⚡ *parallel zu Phase 2*
**Goal of this phase:** Jedes Katalog-Modell kann eine `connections`-Allowlist tragen (leer = alle Connections). Target-Expansion und `model.list` respektieren sie, sodass Codex-Modelle nur über `subscription` und Platform-Modelle nur über `api-key` angeboten werden.
**Can run in parallel with:** Phase 2 (disjunkte Dateien).

- **`Model`-Dataclass:** in [models.py:165](core/models/models.py#L165) Feld `connections: tuple[str, ...] = ()` ergänzen (leer = für alle Connections gültig).
- **Registry-Parsing:** in `ModelRegistry.load` ([models.py:237](core/models/models.py#L237)) `connections=tuple(model_data.get("connections", ()))` setzen.
- **Target-Expansion filtern:** in [model_tasks.py:285](core/model_tasks/model_tasks.py#L285) (Schleife `for connection in usable_connections`) eine Connection für ein Modell **überspringen**, wenn `model.connections` nicht leer ist **und** `connection.id not in model.connections`.
- **`model.list`-Payload:** in [payloads.py:88](server/rpc/payloads.py#L88) (`_model_response`) `"connections": list(model.connections)` ergänzen, damit die WebUI nur gültige Connections anbietet.
  - In [connection_methods.py:53](server/rpc/connection_methods.py#L53) (`_list_models`) bleibt die Provider-Credential-Gate bestehen; keine zusätzliche Filterung nötig (die `connections`-Info reicht der UI).
- Tests: in den `model_tasks`-Tests Expansion mit gemischter Allowlist abdecken (Provider mit zwei usable Connections, ein Modell `connections=["subscription"]`, eines `connections=["api-key"]` → je genau ein Target). In den `models`-Tests Parsing von `connections` (gesetzt + weggelassen → `()`). In den RPC-Tests prüfen, dass `model.list` `connections` ausgibt.
- read: `.vorch/specs/models.md`, `.vorch/specs/model_tasks.md`
- files: [core/models/models.py](core/models/models.py), [core/model_tasks/model_tasks.py](core/model_tasks/model_tasks.py), [server/rpc/payloads.py](server/rpc/payloads.py), [tests/core/models/](tests/core/models/) *(Registry-Parsing-Test)*, [tests/core/model_tasks/](tests/core/model_tasks/) *(Expansions-Filter-Test)*, [tests/server/test_rpc.py](tests/server/test_rpc.py) *(model.list connections)*

**Dependencies:** Phase 1 (für End-zu-End sinnvoll, aber Code-technisch unabhängig).
**Done when:** Ein Provider mit zwei usable Connections und je per-Connection-getaggten Modellen ergibt in `task_model.list_targets` **kein** Cross-Produkt mehr; `model.list` liefert `connections` pro Modell; `python scripts/quality.py core/models/ core/model_tasks/ server/rpc/payloads.py` grün.

---

#### Phase 4: Connection-bewusste Discovery mit Merge
**Goal of this phase:** `model.refresh_db` für `openai` aktualisiert **nur** die Modelle der refreshten Connection und **erhält** die der anderen Connection. Discovery nutzt `models_endpoint` + `base_url` der Connection und taggt geschriebene Modelle mit `connections: [<connection-id>]`.
**Can run in parallel with:** none (editiert `discovery.py` und `connection_methods.py`, beide aus früheren Phasen berührt).

- **`refresh_models`** ([discovery.py:67](core/models/discovery.py#L67)):
  - Endpoint = `credential_connection.models_endpoint or provider_config.models_endpoint`; wenn beides fehlt → `ValueError`.
  - Basis-URL = `credential_connection.base_url or provider_config.base_url` (statt nur `provider_config.base_url` in [discovery.py:87](core/models/discovery.py#L87)).
  - Beim Schreiben jedem Modell-Dict `"connections": [credential_connection.id]` hinzufügen.
  - **Merge statt Überschreiben** ([discovery.py:165-174](core/models/discovery.py#L165)): existierende `<provider>.json` (falls vorhanden) laden; deren Modelle behalten, **deren `connections` die aktuelle `connection.id` NICHT enthält**; die frisch entdeckten (für diese Connection getaggten) Modelle hinzufügen/ersetzen. Ergebnis schreiben.
  - `_model_to_data` ([discovery.py:274](core/models/discovery.py#L274)): `"connections"` mit ausgeben, wenn nicht leer (für den `Model`-Zweig; der `Mapping`-Zweig reicht das Feld durch).
- **Refresh-Caller** ([connection_methods.py](server/rpc/connection_methods.py)):
  - `_refresh_provider_model_db` ([connection_methods.py:290](server/rpc/connection_methods.py#L290)) und `_refresh_global_model_db` ([connection_methods.py:258](server/rpc/connection_methods.py#L258)): statt einer einzelnen "ersten nutzbaren" Connection über **alle** Connections iterieren, die (a) Credentials haben **und** (b) einen effektiven `models_endpoint` (`connection.models_endpoint or provider.models_endpoint`) besitzen, und für jede `refresh_models(..., credential_connection=connection)` aufrufen (akkumuliert per Merge in dieselbe Datei). Registry **einmal** am Ende neu laden ([connection_methods.py:317](server/rpc/connection_methods.py#L317)).
  - Pro-Connection-Credential über das bestehende `provider_access`-Helfer-Muster ([provider_access.py:42-49](server/rpc/provider_access.py#L42)) holen (API-Key bzw. OAuth-Token), nicht über "first usable".
  - Provider ohne **jede** endpoint-tragende Connection im globalen Refresh überspringen (wie bisher der `models_endpoint`-Guard).
- Tests: [test_discovery.py](tests/core/models/test_discovery.py) — (1) Refresh einer Connection taggt Modelle mit deren ID; (2) Merge erhält Modelle einer zweiten Connection; (3) Endpoint/Base-URL werden von der Connection übernommen. RPC-Refresh-Tests in [test_rpc.py](tests/server/test_rpc.py) auf den neuen Multi-Connection-Loop anpassen.
- read: `.vorch/specs/providers.md`, `.vorch/specs/models.md`
- files: [core/models/discovery.py](core/models/discovery.py), [server/rpc/connection_methods.py](server/rpc/connection_methods.py), [server/rpc/provider_access.py](server/rpc/provider_access.py) *(nur falls ein Per-Connection-Credential-Helfer ergänzt werden muss)*, [tests/core/models/test_discovery.py](tests/core/models/test_discovery.py), [tests/server/test_rpc.py](tests/server/test_rpc.py)

**Dependencies:** Phase 1 (`models_endpoint`/`base_url` pro Connection), Phase 2 (Adapter-Map, `discovery_headers` mit Codex-Headern), Phase 3 (`Model.connections`).
**Done when:** Ein `model.refresh_db` für `openai` mit usable `subscription`-Connection schreibt `/codex/models`-Modelle getaggt `["subscription"]` und lässt zuvor vorhandene `["api-key"]`-Modelle in `openai.json` stehen; `python scripts/quality.py core/models/discovery.py server/rpc/` grün.

---

#### Phase 5: Resources & Kataloge zusammenführen, Alt-Artefakte löschen
**Goal of this phase:** Genau ein OpenAI-Provider in den Resources; ein gemergter Modell-Katalog; keine `openai-subscription`-Dateien mehr.
**Can run in parallel with:** none (definiert das Format, das M1-M4 voraussetzen).

- **Provider-JSON** [resources/providers/openai.json](resources/providers/openai.json) editieren:
  - `"adapter": "openai"` (statt `openai_compatible`).
  - `"base_url": "https://api.openai.com/v1"` (Provider-Default = Platform).
  - `connections`:
    1. `api-key` — `type: api_key`, `auth` mit `credential_key: OPENAI_API_KEY` (unverändert). Kein `mode`, kein `models_endpoint`.
    2. `subscription` — `type: oauth`, `label: "ChatGPT Plus/Pro"`, `auth: {header: "Authorization", prefix: "Bearer "}`, **`base_url: "https://chatgpt.com/backend-api"`**, **`mode: "codex_responses"`**, **`models_endpoint: "/codex/models"`**, plus den vollständigen `oauth`-Block aus der alten [openai-subscription.json](resources/providers/openai-subscription.json) (`flow: device`, `device_flow: openai_codex`, `client_id`, `device_auth_url`, `token_url`, `verification_uri`, `redirect_uri`, `expires_in`, `scopes`).
  - Den alten **Platzhalter-Connector `oauth`** (`OPENAI_OAUTH_TOKEN`) **entfernen**.
  - Provider-Level **`extra_headers` entfernen** (Codex-Header sind jetzt adapter-eigen). `defaults` (`max_tokens`, `temperature`) belassen.
  - **Kein** Provider-Level `models_endpoint` (liegt jetzt auf der `subscription`-Connection).
- **Provider-JSON löschen:** [resources/providers/openai-subscription.json](resources/providers/openai-subscription.json).
- **Modell-Katalog mergen** in [resources/models/openai.json](resources/models/openai.json):
  - Bestehendes Platform-Modell `gpt-5.2` → Feld `"connections": ["api-key"]` ergänzen.
  - Alle Modelle aus [resources/models/openai-subscription.json](resources/models/openai-subscription.json) (`codex-auto-review`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.5`) hinzufügen, jeweils mit `"connections": ["subscription"]`. IDs/Capabilities unverändert übernehmen.
- **Katalog-Dateien löschen:** [resources/models/openai-subscription.json](resources/models/openai-subscription.json), [resources/models/openai-subscription.raw.json](resources/models/openai-subscription.raw.json).
- [resources/models/openai.overrides.json](resources/models/openai.overrides.json) prüfen: bezieht sich auf Platform-Modelle, bleibt; nichts Codex-spezifisches hinzufügen.
- Tests: bestehende Tests, die den Provider `openai-subscription` als Resource erwarten, auf `openai` + Connection `subscription` umstellen (siehe Phase 8-Liste; soweit dieselben Dateien wie in früheren Phasen, dort miterledigen).
- read: `.vorch/specs/providers/openai.md`, `.vorch/GLOSSARY.md` (Provider/Adapter/Model)
- files: [resources/providers/openai.json](resources/providers/openai.json), [resources/providers/openai-subscription.json](resources/providers/openai-subscription.json) *(löschen)*, [resources/models/openai.json](resources/models/openai.json), [resources/models/openai-subscription.json](resources/models/openai-subscription.json) *(löschen)*, [resources/models/openai-subscription.raw.json](resources/models/openai-subscription.raw.json) *(löschen)*

**Dependencies:** Phase 1-4 (Format/Adapter/Discovery müssen das neue Schema verstehen).
**Done when:** `ProviderRegistry.load` kennt nur noch `openai` (kein `openai-subscription`); `ModelRegistry.load` liefert `gpt-5.5` unter `("openai", "gpt-5.5")` mit `connections == ("subscription",)`; voller `python scripts/quality.py` grün; `python scripts/quality-frontend.py` grün.

---

#### Phase 6: Manuelle `~/.vbot`-Migration (VON HAND — kein App-Code)
**Goal of this phase:** Der bestehende ChatGPT-Login wird unter der neuen Provider/Connection-ID weiterverwendet, ohne erneutes Einloggen. **Keine** Migrationslogik in der App — dies sind einmalige Datei-Operationen.

- **OAuth-Token umbenennen:** `~/.vbot/oauth/openai-subscription-oauth.json` → `~/.vbot/oauth/openai-subscription.json`.
  - Begründung: neuer Pfad = `<provider_id>-<local_connection_id>.json` = `openai` + `subscription` = `openai-subscription.json`. Token-**Inhalt** bleibt unverändert (`access_token`, `refresh_token`, `expires_at`, `extra.chatgpt_account_id`); der Account-Header wird zur Laufzeit aus dem JWT extrahiert.
- **Agent-Modell anpassen:** `~/.vbot/agents/openai/agent.json` → `"model": "openai-subscription/gpt-5.5::oauth"` ersetzen durch `"model": "openai/gpt-5.5::subscription"`.
- **Restliche Daten scannen:** in `~/.vbot` (`settings.json`, alle `agents/*/agent.json`, `channels/*/channel.json`) nach weiteren Vorkommen von `openai-subscription/` oder dem alten `openai/...::oauth` suchen und auf `openai/...::subscription` umschreiben. (Erwartung: nur `agents/openai/agent.json` betroffen.)
- files: *(Nutzerdaten außerhalb des Repos — keine Repo-Dateien; manuelle Operationen.)*

**Dependencies:** Phase 5 (Provider-ID `openai` + Connection-ID `subscription` final).
**Done when:** Datei `~/.vbot/oauth/openai-subscription.json` existiert (und `...-oauth.json` nicht mehr); `agents/openai/agent.json` zeigt auf `openai/gpt-5.5::subscription`; nach Server-Neustart meldet `provider.connection_status`/`connection.list` die `openai:subscription`-Connection als `connected`/`usable` **ohne** neuen Device-Flow, und ein Chat-Run mit dem `openai`-Agent läuft über `/codex/responses`.

---

#### Phase 7: Specs & Docs
**Goal of this phase:** Dokumentation spiegelt den Ein-Provider-Zustand.
**Can run in parallel with:** kann nach Phase 5 jederzeit (berührt nur `.vorch/`).

- [.vorch/specs/providers/openai.md](.vorch/specs/providers/openai.md): erweitern — dokumentiert jetzt **beide** Connections: `api-key` (`/chat/completions`, Default-Modus) und `subscription` (`mode: codex_responses`, `base_url` chatgpt.com, `models_endpoint /codex/models`). Den Codex-Inhalt aus der alten Subscription-Spec (OAuth-Device-Flow `openai_codex`, ChatGPT-Account-Header, `store:false`, `instructions`-Pflicht, Reasoning-Efforts, adapter-eigene Codex-Header, `/codex/models`-Katalog) hierher übernehmen. `mode`/per-Connection-`models_endpoint`/`base_url` und die Per-Modell-`connections`-Allowlist beschreiben.
- [.vorch/specs/providers/openai-subscription.md](.vorch/specs/providers/openai-subscription.md): **löschen**.
- [.vorch/specs/providers.md](.vorch/specs/providers.md): Satz "Direct OpenAI Platform access and ChatGPT subscription access are separate providers…" → korrigieren auf "ein `openai`-Provider mit zwei Connections (`api-key`, `subscription`)". Child-Spec-Liste: Zeile `providers/openai-subscription.md` entfernen. Data-Model/Conventions um per-Connection `mode` + `models_endpoint` und Per-Modell-`connections`-Allowlist (Discovery-Merge) ergänzen.
- [.vorch/PROJECT.md](.vorch/PROJECT.md): Specs-Index — Zeile `providers/openai-subscription.md` entfernen.
- [.vorch/specs/models.md](.vorch/specs/models.md): `Model.connections`-Feld dokumentieren (leer = alle Connections; bindet Modell an Connection(s)).
- [.vorch/specs/runtime.md](.vorch/specs/runtime.md): falls `openai-subscription`/Adapter-Map erwähnt → auf `openai`-Adapter + `connection_mode`-Durchreichung aktualisieren.
- [.vorch/specs/model_tasks.md](.vorch/specs/model_tasks.md): Expansion respektiert jetzt die `connections`-Allowlist (kein Cross-Produkt mehr).
- **Glossar:** Erwägen, einen `Connection`-Eintrag in [.vorch/GLOSSARY.md](.vorch/GLOSSARY.md) zu ergänzen (Connection = benannte Auth-/Endpoint-Variante eines Providers, kann eigene `base_url`/`mode`/`models_endpoint` haben). Über die `glossary`-Skill triagieren, nicht direkt schreiben.
- **Vor jeder Spec-Bearbeitung** `.vorch/workflows/spec-workflow.md` lesen (CLAUDE.md-Pflicht).
- read: `.vorch/workflows/spec-workflow.md`
- files: [.vorch/specs/providers/openai.md](.vorch/specs/providers/openai.md), [.vorch/specs/providers/openai-subscription.md](.vorch/specs/providers/openai-subscription.md) *(löschen)*, [.vorch/specs/providers.md](.vorch/specs/providers.md), [.vorch/PROJECT.md](.vorch/PROJECT.md), [.vorch/specs/models.md](.vorch/specs/models.md), [.vorch/specs/runtime.md](.vorch/specs/runtime.md), [.vorch/specs/model_tasks.md](.vorch/specs/model_tasks.md)

**Dependencies:** Phase 5 (Endzustand steht fest).
**Done when:** Keine Datei unter `.vorch/` referenziert mehr `openai-subscription` als eigenen Provider; `openai.md` deckt beide Connections ab; Specs-Index in PROJECT.md aktuell.

---

#### Phase 8: Verbleibende Test-Fixtures bereinigen
**Goal of this phase:** Kein Test referenziert mehr den alten Provider `openai-subscription`.
**Can run in parallel with:** nach Phase 5; pro Datei unabhängig (⚡ untereinander, disjunkte Dateien).

Diese Dateien referenzieren `openai-subscription`/`openai_subscription` und sind in früheren Phasen nicht abgedeckt — auf `openai` + Connection `subscription` (bzw. `OpenAIAdapter` mit `connection_mode`) umstellen:
- [tests/server/test_phase3_integration.py](tests/server/test_phase3_integration.py) ⚡
- [tests/core/runtime/test_runtime_integration.py](tests/core/runtime/test_runtime_integration.py) ⚡
- [tests/core/providers/test_auth_flow.py](tests/core/providers/test_auth_flow.py) ⚡ *(Codex-Device-Flow: an `device_flow: openai_codex` gebunden, Provider-ID-Bezug aktualisieren)*
- [tests/core/providers/test_token_getter.py](tests/core/providers/test_token_getter.py) ⚡
- [tests/core/chat/test_chat_loop.py](tests/core/chat/test_chat_loop.py) ⚡
- [webui/src/components/__tests__/AgentsView.test.js](webui/src/components/__tests__/AgentsView.test.js) ⚡
- [webui/src/components/__tests__/DebugView.test.js](webui/src/components/__tests__/DebugView.test.js) ⚡
- [webui/src/components/__tests__/SettingsViewOAuth.test.js](webui/src/components/__tests__/SettingsViewOAuth.test.js) ⚡
- files: jede der oben gelisteten Dateien (disjunkt → parallel)

**Dependencies:** Phase 2-5.
**Done when:** `grep -rn "openai-subscription\|openai_subscription" tests/ webui/` liefert nichts mehr; voller `python scripts/quality.py` und `python scripts/quality-frontend.py` grün.

---

### Done when (gesamt)
- `ProviderRegistry.load` listet `openai` und **kein** `openai-subscription`.
- `get_adapter("openai","openai:api-key")` → `OpenAIAdapter` (chat/completions); `get_adapter("openai","openai:subscription")` → `OpenAIAdapter` (codex/responses).
- `model.list` für `openai` liefert Platform-Modelle mit `connections:["api-key"]` und Codex-Modelle mit `connections:["subscription"]`; `task_model.list_targets` erzeugt kein falsches Cross-Produkt.
- `~/.vbot/oauth/openai-subscription.json` (umbenannt) wird von der `openai:subscription`-Connection genutzt; **kein** erneuter Login; Chat-Run über Codex funktioniert.
- `grep -rn "openai-subscription\|openai_subscription"` über das ganze Repo (außer dieser Plan-Datei) liefert nichts.
- `python scripts/quality.py` und `python scripts/quality-frontend.py` vollständig grün.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Codex-Header (`OpenAI-Beta`/`originator`) gehen versehentlich auch auf `/chat/completions` der `api-key`-Connection | Mittel | Mittel | Header strikt im Codex-Pfad des Adapters bauen; Provider-`extra_headers` entfernen; Test prüft, dass der Default-Pfad diese Header **nicht** sendet |
| Discovery-Merge überschreibt Modelle der anderen Connection | Mittel | Hoch | Merge-Regel: nur Modelle mit der aktuellen `connection.id` ersetzen, andere erhalten; dedizierter Test |
| Token-Datei falsch benannt → erneuter Login nötig | Niedrig | Hoch | Exakter Zielname `openai-subscription.json` (Phase 6); Verifikation via `connection.list`-`usable` vor Abschluss |
| Einheitliche `get_adapter`-Aufrufstelle bricht Adapter ohne `connection_mode` | Mittel | Mittel | `connection_mode` als keyword-only mit Default `None` in `anthropic.py`/`opencode_go.py`/`openai_compatible.py` ergänzen; Runtime-Tests über alle Adapter |
| `agent.json` mit altem Modell-String führt nach Merge zu unbekanntem Provider → Run-Fehler | Niedrig | Mittel | Phase 6 schreibt den String um; PROJECT.md verbietet App-Fallback bewusst, daher manuell |
| `OpenAIAdapter.normalize_catalog_entry` ist codex-geprägt, würde bei künftigem `api-key`-`models_endpoint` falsch normalisieren | Niedrig | Niedrig | Aktuell hat nur `subscription` einen `models_endpoint`; in `openai.md` als Annahme dokumentieren |

### Offene Entscheidung (vom User bereits bestätigt, hier nur dokumentiert)
- **Connection-Namen:** lokale IDs `api-key` und `subscription`. Der alte Platzhalter `openai:oauth` (`OPENAI_OAUTH_TOKEN`) wird ersatzlos entfernt. (User: "mir egal ob es subscription oder oauth oder login … genannt wird".)
