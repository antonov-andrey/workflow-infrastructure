# Production-Среда `Workflow Control Center`

## Назначение

`design/production-environment.md` задаёт обязательную архитектуру будущей production-среды `Workflow Control Center` на AWS. Общие release, Kubernetes, data-plane и security invariants принадлежат `design/environment-model.md`; этот документ владеет только production adapters и acceptance.

Production account ещё не назначен, production resources в рамках текущей разработки не создаются, а этот документ не предоставляет standing authority для AWS mutations. Account `227373271916` является Organizations management account и не является production account WCC.

## Account И Полномочия

Production разворачивается в отдельном Organizations member account. Deployment principal, runtime workload identities, tenant roles и human operator roles разделены. Static access keys и shared administrator credentials workloads запрещены.

До environment deployment production account получает отдельный account-foundation owner общих account-level guards по `design/environment-model.md`. Production environment stacks только проверяют foundation state и не создают competing S3/Lake Formation/Session Manager owners. Account-local AWS names состоят из resource role и environment identity без project prefix; это правило не переименовывает Kubernetes/Product domain objects.

Изменение production требует отдельного явного разрешения пользователя, проверки caller identity/account/region и предварительного inspection exact change set. Development-account standing authority никогда не переносится автоматически.

## Compute И Kubernetes

Production использует Amazon EKS managed control plane не менее чем в трёх Availability Zones. Worker capacity предоставляют managed node groups; Kubernetes control plane, node lifecycle и version upgrades не реализуются собственным k3s bootstrap.

Trusted platform workloads и untrusted publisher/workflow workloads не co-locate. Untrusted images запускаются только на отдельных tainted node groups через production-approved sandbox `RuntimeClass`, обеспечивающий stronger-than-default kernel boundary. Обычный container runtime и одна NetworkPolicy недостаточны для arbitrary third-party code. Пока такая boundary не выбрана, реализована и принята, запуск arbitrary user images в production запрещён.

Каждый workload задаёт requests/limits, affinity/anti-affinity, topology spread, PodDisruptionBudget и rollout strategy в соответствии с реальной доступностью. Stateful singleton controller сохраняет single-writer ownership через lease/fencing и не становится параллельным только ради HA. Horizontal/cluster autoscaling применяется только к stateless или явно partitioned workloads.

## AWS Identity

Каждая trusted AWS workload family получает отдельную EKS Pod Identity association на её common stable ServiceAccount и отдельную least-privilege IAM role. ServiceAccount credentialed materializer/control workload не используется workflow, browser, VPN, source validation, wait Pod или другим credentialless workload. Namespace или service account alone не заменяет trust policy, session tags и resource restrictions. Workload role не получает `AdministratorAccess`.

Production adapter сохраняет common `PlatformCredentialDeliveryMode=ambient`. AWS SDK использует Pod Identity через standard provider chain; production Pods не получают development `credential_process` ConfigMap/Secret, EC2 credential path или file-expiration probe. Это adapter того же common static и dynamic workload graph, а не отдельная production реализация.

Deployment principal может изменять EKS и declarative infrastructure, но не передаётся Pods. Tenant Data/Athena sessions сохраняют mandatory `UserDataRootId` tag, permanent prefix restriction и narrowing session policy из общего data-plane contract.

Untrusted workflow, browser, VPN provider и source-validation Pods не получают Pod Identity, node role credentials, Product DB, Kubernetes API token или platform secrets. Node metadata path и credential endpoints для них закрыты.

## Registry, Persistence И Сеть

Platform и accepted workflow images публикуются в Amazon ECR и используются только по immutable digest. Cross-account либо cross-region replication требует отдельного design и не выводится из mutable tags.

Product relational persistence использует Amazon RDS с encryption, automated backups, point-in-time recovery, Multi-AZ availability и отдельными credentials/roles в owner-local ConfigMap/Secret interfaces каждой workload family. Shared WorkflowRun/VPN/Data DB Secret запрещён. Kubernetes persistent storage использует EBS CSI с encrypted volumes, declared storage classes, snapshots и restore acceptance; local-path и host-retained directory не являются production adapters.

Product HTTP публикуется только через TLS ingress с controlled DNS, certificate lifecycle, request limits, access logging и standard UI security headers. EKS API, database, registry administration и internal platform endpoints не становятся публичными Product routes.

Production network topology разделяет ingress, trusted control plane, data workers, untrusted workflow/browser/VPN workloads и persistence. NetworkPolicy является обязательным дополнительным уровнем к VPC/security-group/IAM boundaries, а не единственным isolation mechanism.

## Release И Recovery

Production использует тот же Product source resolver, source graph и environment-neutral Kubernetes base, что development. CI/deployment boundary разрешает moving sources один раз, собирает exact images и сохраняет immutable source/image/render manifest до rollout.

Recovery и rollback не выполняют Git lookup, package resolution или image rebuild. Они используют exact retained release artifacts и ECR digests. Production rollout поддерживает bounded rollback, database compatibility конкретного release и отсутствие mixed contract versions между platform-owned consumers.

Development local registry, retained host release directory, SSM-only HTTP tunnel, single-node PostgreSQL, local-path storage, idle auto-stop и broad EC2 platform role не переносятся в production.

## Доступность И Наблюдаемость

Production acceptance требует multi-AZ failure tolerance, controlled node drain, safe controller leader/fencing behavior, disruption budgets, backup restore, credential rotation без restart и отсутствие mixed releases.

Metrics, logs, traces и audit используют отдельные least-privilege sinks, redaction и retention. Diagnostic access не раскрывает Secret content, temporary credentials, user Data или raw VPN configuration.

## Запрещённые Обходы

- Создание production resources до назначения account и явного разрешения пользователя.
- Копия Product implementation или Kubernetes manifest tree специально для production.
- `AdministratorAccess` у Pod, shared workload role или EC2-style platform credential distribution.
- Co-location trusted и arbitrary untrusted workloads на одном node group.
- Запуск arbitrary image без approved sandbox RuntimeClass.
- Mutable image tags, Git resolution или package resolution при recovery.
- k3s, local registry, local-path persistence, public SSH или host-bound secrets как production architecture.

## Проверки

До первого production apply design acceptance обязана доказать exact account boundary, full common/adapters render, Pod Identity least privilege, node-group isolation, sandbox RuntimeClass, ECR immutable digests, RDS/EBS backup recovery, TLS ingress и data-plane tenant isolation.

Live production acceptance определяется отдельной утверждённой goal после назначения account. Development smoke, unit tests или успешный CloudFormation validation не заменяют её.
