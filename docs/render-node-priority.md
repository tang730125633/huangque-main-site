# Render-node primary and standby routing

The relay already supports `RELAY_PRIORITY_NODES` (comma-separated node names)
and `RELAY_PRIORITY_WINDOW` (seconds). Empty priority names preserve equal-tier
dispatch. Priority is a relay configuration, not a renderer concurrency setting.

For the current operator-requested pool, use primary names `tang,yuelei,bf3060`
and a 20-second window. Keep each primary renderer and poller at concurrency5;
set hy3050ti's renderer and poller to concurrency1. Other nodes are not promoted
to the primary tier by this setting and their service configurations need not
change.

An idle poller asks for work roughly every5 seconds. A non-primary yields while
an eligible, non-cooled-down primary has asked within the priority window. When
primaries are full/offline and stop asking, standby can claim after that window.
This is recent pull-demand evidence, not an exact GPU utilization measurement;
it may introduce up to about20 seconds of backup activation delay. Heartbeats do
not count as spare-slot requests. Existing GPU/template admission checks remain.

Load balancing among primaries must ignore idle standby nodes, or a primary with
one running job could be blocked from using its remaining slots while standby is
also denied by priority. The normal balance between eligible primary nodes is
preserved. Standby nodes retain the existing ordinary balancing behavior.

Activation: drain jobs; back up relay code/env and SQLite through the backup API;
deploy the pushed/merged relay file only; set the two routing values without
printing or altering other environment values; restart only the relay. Wait for
primary nodes to register before resuming the standby poller. Confirm `/health`
reports the active priority names/window, node readiness and zero unexpected
backlog. Do not insert production jobs just to test policy: use the HTTP tests
with an isolated temporary database. Rollback restores the previous relay/env
and node concurrency settings after draining.
