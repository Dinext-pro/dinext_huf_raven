[Русский](README.ru.md)

<!-- dinext-presentation:start -->
<p align="center">
  <a href="https://dinext.ru"><img src="docs/assets/readme/dinext-logo.png" alt="Dinext" width="128"></a>
</p>
<h1 align="center">Dinext Huf Raven</h1>

<hr>

<p align="center">Planned integration of Dinext chat and AI agents. This repository currently contains the integration task and boundaries, not a completed application.</p>

<p align="center">
  <img src="docs/assets/readme/overview.png" alt="The existing Dinext chat interface with fictional demo participants. The AI integration described here is still planned." width="960">
</p>
<p align="center"><sub>The existing Dinext chat interface with fictional demo participants. The AI integration described here is still planned.</sub></p>

<p align="center">
  <a href="https://dinext.ru">Website</a> · <a href="https://docs.dinext.ru">Documentation</a>
</p>
<!-- dinext-presentation:end -->

# Dinext Huf Raven

Dinext chat assistant integration: [Raven](https://github.com/frappe/raven) provides the user conversation interface, while [Huf](https://github.com/Dinext-pro/huf) provides the infrastructure for AI agents and task execution. This repository owns **all Dinext enhancements connecting Raven and Huf**. The Huf fork retains its connection to the [original project](https://github.com/tridz-dev/huf) and hosts necessary changes to the Huf core.

## Task

An employee contacts the assistant in an existing Raven direct conversation or channel. The assistant accepts the task, asks for clarification when needed, reads the Dinext data available to that employee, and performs authorized actions through Huf. Progress, confirmation requests, and results remain in the original Raven thread. Information that cannot be shown to every channel participant is returned to the employee privately.

Development includes:

1. An inbound Raven adapter for direct messages, explicit channel mentions, thread replies, and attachments, with protection against duplicate message processing and replies to the bot itself.
2. Reliable mapping of the Raven message author to a Frappe user. Tools and queries run with that user's permissions, while channel access and the response audience are checked separately.
3. Connecting the Huf agent, its models, tools, memory, execution queue, logs, limits, and action confirmation mechanism to the Raven conversation.
4. Dinext tools for documents, search, metadata, reports, approvals, workflows, files, analytics, and visualization. The coverage baseline is the user functionality of the previously used MCP FAC, subject to the individual employee's permissions.
5. Tests for recovery after a failure, repeated event delivery, stale confirmations, revoked access, and safe publication of results in a group conversation.

## Repository boundaries

| Repository | Responsibility |
| --- | --- |
| [`Dinext-pro/dinext_huf_raven`](https://github.com/Dinext-pro/dinext_huf_raven) | The Raven adapter, Dinext integration tools and rules, tests, and task documentation |
| [`Dinext-pro/huf`](https://github.com/Dinext-pro/huf) | A native Huf fork for minimal core changes that cannot be implemented in the integration |
| [`frappe/raven`](https://github.com/frappe/raven) | The original chat application and its user interface |

Changes are planned in the central OpenSpec store; this repository contains only the `openspec/config.yaml` pointer and standard agent instructions. The Dinext working line is `develop`, and the stable line is `main`. The application has not yet been implemented or installed: the current stage creates a public development repository and records the task.

## Acceptance of the first working integration

- A direct message and an `@` mention in a channel each start exactly one Huf task and receive a response in the corresponding Raven thread.
- Two users with different permissions see only the data they are allowed to access; confidential results do not appear in a group channel.
- A proposed document change can be confirmed or rejected in Raven; after confirmation, the verified operation result is visible.
- A task survives repeated message delivery and executor restarts without duplicating ERP writes.
- A capability matrix demonstrates coverage of the FAC functionality required by users through native Huf tools and extensions in this repository.

## English summary

Dinext uses Raven for employee conversations and Huf for AI agents and task execution. This repository owns the Dinext-specific Raven↔Huf integration. It will connect direct messages, channel mentions and threads to Huf runs, enforce the requester's Frappe permissions, keep approvals and results in Raven, and verify functional coverage against the earlier FAC capability baseline. The repository currently contains the project charter and development workflow files; the integration is not implemented yet.
