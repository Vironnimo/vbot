# Terminal sharing

The `terminal` Extension supplies native Terminal execution from the running Desktop app to its connected server. Agents use the existing `terminal` Tool, and users manage programs in the app's Terminals view.

The user enables `Terminal` under `Share this computer` in the Desktop app, then allows and selects that Computer for the Session. Sharing starts disabled and belongs to the connected server. An Agent also needs access to the `terminal` Tool; remote Agent Terminals require full access under the Session's Permission Rules. Other Tools, including file access and shell commands, run on the server.

The Desktop can stay minimized while sharing. Quitting it or turning off Terminal sharing closes its Terminal programs, with local confirmation when programs are running or starting. A temporary network disconnect keeps programs alive; inspect the existing Terminals after reconnect before deciding whether to start another.
