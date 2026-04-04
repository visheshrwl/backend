# Playbooks

Operational runbooks for common backend engineering scenarios. Each playbook is a step-by-step guide for a specific situation.

## Available Playbooks

| Playbook | When to Use |
|----------|-------------|
| [incident-response.md](incident-response.md) | During a production incident |
| [performance-tuning.md](performance-tuning.md) | After an incident, or when latency degrades |

## Playbook Principles

1. **Time-boxed steps:** Each step should complete in under 5 minutes
2. **Decision trees:** Binary decisions, not ambiguous judgment calls
3. **Rollback instructions:** Every change has a revert path
4. **Escalation criteria:** Clear thresholds for escalating to on-call lead
