# Инфраструктура `Workflow Control Center`

## Назначение

`DESIGN.md` является корневым архитектурным контрактом и маршрутизатором требований проекта `workflow-infrastructure`. Проект владеет общей моделью сред, облачной инфраструктурой, Product-release delivery, средой разработки, требованиями к будущей production-среде, доступом оператора, восстановлением и локальной оркестрацией развёртывания `Workflow Control Center`.

Продуктовые сущности, API, UI, Kubernetes-манифесты, Product images, ZITADEL, GlitchTip и поведение `WorkflowRun` принадлежат проекту `workflow-control-center`. `workflow-infrastructure` использует их опубликованные интерфейсы и не создаёт вторую реализацию.

## Граница Репозитория

Один репозиторий владеет инфраструктурой всех сред `Workflow Control Center`. Среды разделяются отдельными аккаунтами, стеками, конфигурацией и release identity, но не отдельными проектами `workflow-infrastructure-devel` и `workflow-infrastructure-production`.

Product implementation остаётся единственной. Environment-neutral Kubernetes resources, release graph и data-plane semantics не копируются между development и production; среда выбирает только явно определённые adapters для identity, registry, persistence, ingress, scheduling и lifecycle.

`marketplace-infrastructure` владеет только инфраструктурой маркетплейсов и не содержит шаблоны, инструкции, design-документы или forwarding paths `Workflow Control Center`.

## Принципы

- Инфраструктура определяется декларативно через `CloudFormation`; консоль и ad hoc команды не становятся постоянным владельцем состояния.
- Стабильный data plane и изменяемый compute plane принадлежат разным стекам, чтобы замена узла не затрагивала S3, KMS, IAM, Glue, Lake Formation и Athena.
- Кластер восстанавливается из чистых точных исходников и отслеживаемых Product-манифестов; host bootstrap не содержит исходники приложения, Product secrets или скрытую deployment policy.
- Постоянное состояние хранится отдельно от заменяемого узла и временного build/runtime cache.
- Доступ оператора проходит через AWS Systems Manager без входящих портов и долгоживущих host credentials.
- Каждый release связывает точные исходники, целевую OCI platform, image digests и применённый набор Kubernetes-манифестов.
- Moving source selector разрешается ровно один раз при создании нового Product release, после чего release и recovery используют только сохранённую immutable identity.
- Стоимость контролируется утверждённой архитектурой и накопительным cost checkpoint, а не одним фиксированным месячным лимитом.

## Маршрутизация Требований

| Требование | Документ-владелец |
| --- | --- |
| Общая Product implementation, environment identity, release-source resolution, общие Kubernetes/data-plane/security invariants и adapters сред | `design/environment-model.md` |
| Аккаунт разработки, AWS stacks, EC2, VPC, EBS, Session Manager, k3s, остановка, восстановление и cost checkpoint | `design/development-environment.md` |
| Будущий production account, Amazon EKS, production identity/registry/persistence/ingress, HA и изоляция недоверенных workloads | `design/production-environment.md` |
| Команды оператора, подключение, развёртывание, диагностика и восстановление реализованной среды | `docs/development-environment-operations.md` |
| Product Kubernetes manifests, Product images, ZITADEL, GlitchTip и прикладная приёмка | `workflow-control-center` |
| Data, Secret, Result, Athena, Glue и Lake Formation Product semantics | `workflow-control-center` — `design/data-storage.md` |
| `WorkflowSourceVersion` build, runtime image и `WorkflowRun` execution semantics | `workflow-control-center` — `design/workflow-runtime.md` |
| Browser process и Playwright runtime | `browser-runtime` |
| VPN gateway и provider runtime | `vpn-runtime` |
| Инженерный workflow и границы AWS-изменений | `AGENTS.md` |

## Проверяемость

Каждое изменение инфраструктуры проверяется на фактической среде, которой оно владеет. Проверка синтаксиса шаблона, успешный статус stack или готовность EC2 не заменяют проверку доступа через Session Manager, развёрнутого Product, сохранения состояния, восстановления, целевой OCI platform и стоимости.
