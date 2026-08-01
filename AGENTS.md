# Repository Guidelines

## Table Of Contents

- [Required Standards](#required-standards)
- [Project Contract](#project-contract)
- [Key Directory Map](#key-directory-map)
- [AWS Execution Boundary](#aws-execution-boundary)
- [Secrets And Sensitive Data](#secrets-and-sensitive-data)
- [Change Workflow](#change-workflow)
- [Verification](#verification)
- [Destructive Change Rules](#destructive-change-rules)

## Required Standards

- `project-standards:aws-cloudformation-developer`
- `project-standards:docker-compose-developer`
- `project-standards:http-api-client-developer`
- `project-standards:kubernetes-developer`
- `project-standards:legacy-python-maintainer`
- `project-standards:project-documentation-developer`
- `project-standards:project-foundation`
- `project-standards:project-instruction-developer`
- `project-standards:project-standard-audit`
- `project-standards:pytest-developer`
- `project-standards:python-cli-developer`
- `project-standards:python-developer`
- `project-standards:python-logging-developer`
- `project-standards:python-retry-developer`
- `project-standards:react-ui-developer`
- `project-standards:rest-api-server-developer`
- `project-standards:runtime-config-developer`
- `project-standards:sqlalchemy-developer`
- `project-standards:submodule-developer`
- `project-standards:typescript-developer`
- `project-standards:zitadel-developer`
- `workflow-container-agent-tools:workflow-container-developer` applies to workflow-container platform integration, runtime image platform selection, and deployment boundaries.

## Project Contract

- `DESIGN.md` is the stable architecture entrypoint for Workflow Control Center infrastructure.
- `design/development-environment.md` owns only the development-account, EC2, k3s, retained-state, access, deployment, recovery, and cost specialization.
- `design/environment-model.md` owns the common environment model, Product-release source resolution, shared infrastructure invariants, and the boundary between environment-neutral infrastructure and environment adapters.
- `design/production-environment.md` owns the required future production specialization on Amazon EKS; it does not authorize or provision production resources.
- `docs/development-environment-operations.md` owns the maintained operator workflow for the implemented development environment.
- `cloudformation/**` owns declarative AWS resources. Lasting AWS resources must not be created only through the console or ad hoc commands.
- `development_environment_manage.py` is the primary operator entrypoint of this infrastructure product.
- `workflow_infrastructure/**` owns the main Python implementation for provisioning, lifecycle, source delivery, deployment, diagnostics, and recovery.
- `tool/**` owns only auxiliary project-maintenance commands; Product orchestration MUST NOT be placed there.
- `workflow-control-center` owns Product Kubernetes manifests, Product images, ZITADEL, GlitchTip, application smoke checks, and Product behavior. This repository orchestrates those owners without copying their implementation.
- `marketplace-infrastructure` is unrelated to Workflow Control Center and must not own, forward, or duplicate this repository's contracts or resources.

## Key Directory Map

```text
project/
  AGENTS.md
  cloudformation/
    workflow-control-center-development-compute.yaml
    workflow-control-center-development.yaml
  DESIGN.md
  design/
    development-environment.md
    environment-model.md
    production-environment.md
  development_environment_manage.py
  docs/
    development-environment-operations.md
  .spec/
  test/
  tool/
    venv_create.py
  workflow_infrastructure/
    development_environment/
      composition.py
      host/
      product/
  .worktree/
  worktree-bootstrap.toml
```

- `AGENTS.md`: repository-root canonical instruction owner.
- `cloudformation/`: declarative AWS stack templates for Workflow Control Center environments.
- `cloudformation/workflow-control-center-development-compute.yaml`: compute, network, retained-volume, snapshot, instance-profile, and lifecycle stack for the development environment.
- `cloudformation/workflow-control-center-development.yaml`: stable data-plane stack for the development environment.
- `DESIGN.md`: root architecture router.
- `design/`: stable infrastructure design owners.
- `design/development-environment.md`: canonical development-environment architecture.
- `design/environment-model.md`: canonical common environment and Product-release architecture.
- `design/production-environment.md`: canonical future production-environment architecture.
- `development_environment_manage.py`: canonical primary orchestration entrypoint.
- `docs/`: maintained operator documentation.
- `docs/development-environment-operations.md`: canonical development-environment operations guide.
- `.spec/`: harness-neutral task-artifact root whose reusable semantics are owned by `agent-workflows:goal-brainstorm`.
- `test/`: verification code for infrastructure orchestration and templates.
- `tool/`: auxiliary project-maintenance code only.
- `tool/venv_create.py`: direct Python 3.14 virtual-environment recreation utility.
- `workflow_infrastructure/`: importable `Main project code` package.
- `workflow_infrastructure/development_environment/`: cohesive development-environment subsystem package; shared primitives and composition live at this level, while host and Product release responsibilities live in their explicit child packages.
- `.worktree/`: task-worktree container whose reusable semantics are owned by `agent-workflows:goal-brainstorm`.
- `worktree-bootstrap.toml`: repository bootstrap-resource binding for the reusable manifest contract owned by `agent-workflows:goal-brainstorm`.

## AWS Execution Boundary

- Development uses account `463564115167`, region `us-east-1`, and local profile `workflow-control-center-devel`.
- The user has granted standing authority for any necessary change inside account `463564115167`; do not pause for separate approval there, and report every material mutation at handoff.
- Account `227373271916` is the AWS Organizations management account, not a Workflow Control Center production account.
- The future Workflow Control Center production account has not been assigned. Development-account standing authority does not extend to that future account, account `227373271916`, or any other account.
- Before every AWS mutation, verify the exact caller identity, region, target stack, live parameters, outputs, and drift.
- The one explicitly approved deletion of the obsolete Workflow Control Center development Budget in management account `227373271916` belongs only to the development-environment rollout and does not create standing management-account authority.

## Secrets And Sensitive Data

Never commit or print:

- AWS access keys, session tokens, SSO cache files, exported credentials, or credential-process output;
- SSH private keys, EC2 key material, Product runtime secrets, Kubernetes `Secret` values, database dumps, or identity exports;
- ZITADEL, GlitchTip, registry, VPN, Data, Secret, or source-map credentials;
- CloudFormation or diagnostic output containing secret values.

The EC2 host must use its instance profile and standard temporary AWS credential chain. The local operator uses IAM Identity Center only for CloudFormation and Session Manager control.

## Change Workflow

1. Read `DESIGN.md`, the complete affected thematic design, the complete affected template or orchestration module, and the owning Product contract when the change crosses into `workflow-control-center`.
2. Verify the AWS identity, region, stack state, parameters, outputs, drift, retained-resource identities, and current cost checkpoint.
3. Make the smallest complete source change without duplicating Product deployment logic.
4. Run targeted local validation and inspect the exact source diff.
5. Commit and push the exact clean source state before creating a change set or release.
6. Create and inspect the exact CloudFormation change set, including replacements, deletions, IAM changes, retained-volume effects, and projected recurring and one-time cost deltas.
7. Execute development changes under the standing account authorization; production and other-account mutations require their own explicit authorization.
8. Verify live resources, lifecycle behavior, access paths, deployment identity, recovery state, and Product acceptance after mutation.

## Verification

- Python tooling uses a reproducible Python 3.14 virtual environment and must also work when invoked through a compatible system Python.
- Run targeted tests for changed orchestration behavior, `git diff --check`, and semantic reread of every changed owner contract.
- Run `cfn-lint` and `aws cloudformation validate-template` for each changed template, then inspect the exact change set before execution.
- Infrastructure acceptance must exercise the real Session Manager path, the exact deployed k3s node, clean-source delivery, immutable image digests, Product readiness, idle-stop lease, and all three retained-state recovery scenarios from `design/development-environment.md`.
- A successful template validation or stack status does not replace live Product behavior, access-isolation, recovery, or cost-boundary verification.

## Destructive Change Rules

- Development-account destructive changes are allowed by the standing authorization only when they preserve the retained-state and recovery contracts or intentionally execute the approved pre-production reset.
- A destructive development operation must inventory the exact affected stack, instance, volume, snapshot, bucket, object versions, catalog resources, and Product state before mutation and report the result at handoff.
- Production, Organizations, Identity Center, management-account, account-membership, and billing-boundary destructive changes require explicit authorization unless the exact operation was already approved for the current goal.
