# Общая Модель Сред `Workflow Control Center`

## Назначение

`design/environment-model.md` владеет общими инфраструктурными инвариантами всех сред `Workflow Control Center`: единственной Product implementation, идентичностью среды, созданием Product release, разрешением moving source, общей Kubernetes-границей, data-plane security и точками специализации среды. Конкретный EC2/k3s development contract принадлежит `design/development-environment.md`, а обязательный будущий Amazon EKS production contract — `design/production-environment.md`.

Продуктовые сущности, API, UI, runtime, Data revisions и Workflow semantics принадлежат `workflow-control-center`. Этот документ определяет способ их одинакового развёртывания в разных средах и не дублирует Product behavior.

## Единая Product Implementation

Development и production используют один и тот же Product source graph, API/UI implementation, controllers, workers, runtime contracts, image graph и environment-neutral набор Kubernetes resources. Полная копия Product manifests, альтернативная ветка backend/frontend или отдельный runtime implementation для одной среды запрещены.

Среда предоставляет только typed adapters:

- AWS account, region, identity и credential delivery;
- OCI registry и image publication;
- relational persistence и backup/restore;
- Kubernetes storage classes и persistent-volume behavior;
- ingress, DNS, TLS и operator access;
- node placement, autoscaling, disruption и high availability;
- environment-specific lifecycle и cost controls.

Environment-neutral base не содержит physical account IDs, bucket names, host paths, local registry endpoints, k3s-only storage classes, development DNS names или production ingress values. Adapter values входят в exact deployment render и Product release manifest.

## Идентичность Среды

Каждая инсталляция имеет immutable environment class `development` или `production` и stable environment name. Environment name входит в stack names, ownership tags, release identity, retained roots и operator selection, но не меняет Product domain identity.

Development environment с name `primary` является default. Если в будущем требуется параллельная работа над независимой задачей, создаётся полная изолированная development environment с другим name: отдельные data-plane stacks/resources, compute resources, retained state, registry, release pointer, credentials, tunnel и lifecycle controller. Несколько задач не разделяют один mutable cluster, Product database, Data/Athena state или release root скрыто. Этот контракт не создаёт вторую environment сейчас.

Account-global guards, которые физически не могут существовать отдельно для каждой environment, не становятся environment-local data plane. Account-level S3 Block Public Access и Lake Formation default settings имеют одинаковое fail-closed состояние для всех development environments и сохраняются при удалении environment stack. Только `primary` stack владеет `DataLakeSettings`: он сохраняет exact существующие deployment principal и broad primary platform role в global administrator list, потому что их удаление потребовало бы запрещённой замены уже принятого account-global state. Другая development environment не создаёт и не изменяет `DataLakeSettings`; её platform role получает явные catalog, data-location, database и table permissions только на собственные ресурсы. Поэтому добавление environment не переписывает global authority либо authority другой environment.

Production installation принадлежит отдельному AWS account. Один production cluster может содержать несколько deployment stages только если их data plane, identity, secrets, namespaces, release state и operator authority доказанно разделены; stage не выводится из namespace alone.

## Разрешение Source Для Product Release

Reusable source, который должен автоматически обновляться для каждого нового Product release, объявляется один раз как repository URL и moving selector. Для `workflow-container-contract` standard selector — `HEAD` default branch удалённого repository; имя default branch не предполагается равным `main`.

Resolver выполняется ровно один раз в начале создания release candidate и до любого Product image build:

1. получает advertised symbolic default branch и разрешает selector в exact commit;
2. экспортирует exact tracked tree без `.git`, credentials и неотслеживаемых файлов;
3. фиксирует repository URL, requested selector, resolved symbolic ref, exact commit SHA, package version при её наличии, archive/content digest и полный file manifest;
4. помещает exact tree в immutable source graph данного release.

Все platform-owned consumers одного release, включая WCC backend image, `browser-runtime` и optional platform base image, получают один и тот же exact staged tree через named build context. Runtime dependency file не содержит VCS dependency на moving branch, а image build не выполняет Git/network resolution и не устанавливает сначала moving package, чтобы затем перекрыть его exact copy.

Product release передаёт backend exact digest собранного optional platform base. Backend сохраняет его при создании каждой WorkflowSource build attempt; digest входит в immutable build-input identity. Untrusted source builder получает reserved `WORKFLOW_PLATFORM_BASE_IMAGE` только как exact digest reference через environment adapter read-only repository boundary. Source может игнорировать аргумент, поэтому platform base не становится обязательным интерфейсом. Product update не меняет уже сохранённый attempt digest или его retry.

Неудачная resolution, несовместимость последнего source или failure любого обязательного build/test отклоняет весь новый release candidate. Неявный fallback на предыдущий contract запрещён. Exact source override допустим только по явному указанию пользователя для конкретного release; manifest сохраняет override, requested identity и причину.

Уже принятый release, rollback и recovery никогда не повторяют Git resolution. Они используют сохранённый exact source graph, image digests и deployment render. Следующий новый release снова разрешает current remote `HEAD`, поэтому обновление происходит автоматически без размножения commit pins по consumer repositories.

Moving resolution применяется только к platform-owned release dependencies. Пользовательский `WorkflowSource` остаётся language-neutral, самостоятельно разрешает свой сохранённый Git selector при создании `WorkflowSourceVersion` и не получает Python dependency либо platform base image принудительно.

До появления production и persisted customer state платформа имеет ровно одну текущую техническую version каждого source manifest, Product release manifest и host/operator CLI. Отсутствующая, прежняя либо неизвестная technical version отклоняется; readers нескольких technical shapes, compatibility aliases, sanitizers и transition-only runtime branches запрещены. Универсальные workflow-input migration graph/loader, database migration verifier, domain versioning, rollback и recovery остаются механизмами текущей системы и не считаются legacy. Immutable Product releases и доменные `WorkflowSourceVersion`, созданные по одному текущему contract, являются обычной release/domain history, а не разрешением legacy format.

Изменение текущего technical manifest или runtime contract в pre-production выполняется destructive reset: Product databases, Product object versions, dynamic catalog, workflow volumes, registry и retained Product release/runtime graph очищаются, а новый current-format release создаётся заново. ZITADEL и GlitchTip identity/observability state могут сохраняться отдельным доказанным logical boundary. Конкретные transition scripts и compatibility branches для удалённого state запрещены; это не удаляет reusable механизмы, через которые будущая current implementation объявляет и проверяет migrations, versions, rollback или recovery.

## Общая Kubernetes Граница

Environment-neutral resources задают namespaces, service accounts, workload ownership, secrets/config interfaces, health contracts, security contexts, resource requests/limits, release labels и NetworkPolicy для untrusted/runtime trust boundaries, выразимых одинаково во всех средах. Каждый trusted AWS workload family имеет собственные stable ServiceAccount, ConfigMap и Secret interfaces; credentialed control/materialization workload никогда не разделяет ServiceAccount или DB Secret с workflow, browser, VPN, source validation, wait либо другой workload family. Environment adapter связывает только эти exact identities с provider-specific roles/credentials и материализует registry, storage, ingress, scheduling и trusted-control-plane network topology.

AWS SDK workloads используют standard credential provider chain через один явный environment adapter. Common runtime по умолчанию выбирает `ambient`: common manifests и динамически создаваемые Jobs не содержат projected credential Secret, EC2 path или credential-specific readiness. Development adapter выбирает `credential_process`, монтирует temporary credential file как directory без `subPath` и добавляет fail-closed credential readiness; production adapter оставляет `ambient` и предоставляет EKS Pod Identity без file projection. Выбор adapter передаётся одной environment-owned настройкой всем trusted dynamic workload families; неявное определение среды внутри Product runtime запрещено. Workload-specific health остаётся common contract независимо от способа выдачи AWS identity.

Одна user-source build attempt получает один disposable rootless BuildKit Pod. Он не является общим daemon, не переживает attempt и не разделяет mutable cache или process namespace между publishers. Builder не получает Docker socket, host mount, Kubernetes API token, Product DB, Data, AWS credential chain или runtime secrets. Registry credential существует только в отдельной минимальной push boundary.

OCI registry является adapter. Development может использовать retained cluster-local registry; production использует managed registry. Runtime identity всегда является immutable digest, независимо от реализации registry.

Все source-owned workloads явно задают service account, token automount, security context и resource requests/limits. Untrusted publisher/runtime workload всегда получает exact ingress/egress `NetworkPolicy`; adapter-owned persistence/registry и cross-trust endpoints получают adapter-specific policy. Полная trusted-control-plane segmentation зависит от environment topology и поэтому не кодируется фиктивным common allowlist. Привилегия, host namespace/mount, wildcard egress или Kubernetes API access разрешаются только конкретному platform workload с документированным contract.

## Общий AWS Data Plane

Product Data, Secret, Result, Athena, Glue и Lake Formation semantics принадлежат `workflow-control-center/design/data-storage.md`. Infrastructure предоставляет один параметризованный data-plane template и environment-specific account/names/trust values; отдельные несовместимые development/production реализации запрещены.

State-bearing S3 buckets и customer-managed KMS keys используют `DeletionPolicy: Retain` и `UpdateReplacePolicy: Retain`. CloudFormation delete/replacement не является Product purge. Намеренный reset или permanent purge выполняется отдельной доказанной операцией с inventory exact ресурсов.

Data и Secret buckets не получают blanket expiration всех noncurrent versions. Reference-aware Product reconciliation удаляет orphaned versions после 24 часов и historical versions после 30 дней только при отсутствии retained reference. S3 Lifecycle остаётся только для prefix/object classes, где время само является authoritative contract: incomplete multipart uploads, completed `data-download`, abandoned Athena result и Observability artifacts.

Account-level S3 Block Public Access обязателен дополнительно к bucket-level Block Public Access и policies.

Tenant data access использует defense in depth:

- permanent role policy ограничивает S3 prefix через mandatory `UserDataRootId` principal/session tag; role trust требует один 32-символьный value, AWS STS API не принимает IAM wildcard-символы `*`/`?` в session-tag value, а Product выдаёт только canonical `32 lowercase hex`;
- выдаваемая session policy дополнительно сужает exact operation и path;
- bucket policy, KMS, IAM catalog prefix и Lake Formation должны одновременно разрешить операцию;
- отсутствие или mismatch tag закрывает доступ.

Platform administrator authority не является tenant isolation mechanism. Development может иметь явно документированное broad platform role только как исключение isolated development account; production workloads используют отдельные least-privilege identities.

## Матрица Сред

| Граница | Development | Production |
| --- | --- | --- |
| Compute | Один `m7g.xlarge`, single-node k3s | Amazon EKS, multi-AZ managed node groups |
| Operator access | Session Manager и SSM tunnels, без ingress | Отдельный deployment authority и TLS ingress |
| Workload AWS identity | Renewable EC2 instance-profile credentials для trusted platform workloads | EKS Pod Identity на каждый workload |
| OCI registry | Retained cluster-local registry | Amazon ECR |
| Relational persistence | Retained local PostgreSQL | Amazon RDS |
| Kubernetes persistence | Retained EBS и local-path adapter | EBS CSI и production backup/restore |
| Availability | Один узел, без HA, auto-stop | HA, rollout, disruption и autoscaling contracts |
| Untrusted workloads | Принятый development risk: один node, rootless/process/network isolation в выделенном account | Отдельные tainted nodes и approved sandbox RuntimeClass |

Матрица описывает adapters, а не две реализации Product.

## Проверки

Contract tests обязаны доказывать отсутствие environment-specific values в common Kubernetes base, полный render каждого adapter, один exact contract source во всех consumers release, отсутствие VCS resolution внутри image build, strict current technical manifest versions и recovery без network/source resolution или преобразования retained bytes.

AWS acceptance проверяет Retain policies, account-level и bucket-level public-access blocks, tagged tenant prefix policy вместе с narrowing session policy и реальную изоляцию двух users. Kubernetes acceptance проверяет обновление credential directory без restart, disposable builder per attempt, отсутствие credential/token у untrusted workloads и точную registry NetworkPolicy.

Semantic acceptance отдельно проверяет, что development и production docs не дублируют Product implementation и не выдают development exceptions за production requirements.
