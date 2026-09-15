# Generation Channel Control

This context names the product operations, routing choices, and durable evidence used to control AI image and video generation across the web application, agents, and administration console.

## Language

**Operation**:
A stable customer-visible generation action identified by an `operation_id`. Its identity does not change when display labels, suppliers, channels, or models change.
_Avoid_: Front, kind, provider action

**Channel**:
One configured connection to a supplier protocol, including its Adapter, endpoint, credential, actual model, limits, and health evidence.
_Avoid_: Provider, model, line

**Operation Mapping**:
A published choice of primary and optional backup Channels for one Operation.
_Avoid_: Front mapping, model mapping

**Mapping Revision**:
An immutable version of an Operation Mapping. Publishing or rolling back always creates a new revision.
_Avoid_: Current mapping, updated mapping

**Channel Version**:
An immutable version of a Channel configuration. Editing or rolling back a Channel creates a new version.
_Avoid_: Channel revision

**Execution Snapshot**:
The server-owned record of the Operation, Mapping Revision, Channel Version, actual model, price, and invocation source chosen before a task is charged and created.
_Avoid_: Binding, live config

**Invocation Source**:
The trusted product entry that initiated an Operation, such as web, agent, administrator test, canvas, or director workflow.
_Avoid_: Client source

**Execution Evidence**:
The durable link from an Operation and task to the actual Channel, supplier task identifier, stage, duration, outcome, and artifact.
_Avoid_: HTTP success, health only

**Control State**:
The routing enforcement state of an Operation: legacy, shadow, managed, or paused.
_Avoid_: Enabled flag
