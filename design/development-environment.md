# Среда Разработки `Workflow Control Center`

## Назначение

`design/development-environment.md` владеет архитектурой AWS-аккаунта разработки, стеков `CloudFormation`, вычислительного узла, k3s, постоянного состояния, доступа оператора, доставки исходников, сборки images, AWS credentials, автоматической остановки, восстановления и контроля стоимости. Product contracts данных и runtime принадлежат `workflow-control-center`; этот документ определяет только их инфраструктурную реализацию.

## Аккаунт И Полномочия

Единственная облачная среда разработки и AWS acceptance находится в Organizations member account:

- organization `o-zsa5oqkb8u`;
- OU `Sandbox` (`ou-v7ai-q93m9tv7`);
- account name `workflow-control-center-devel`;
- account email `a.antonov+workflow-control-center-devel@apwid.com`;
- account ID `463564115167`;
- region `us-east-1`;
- IAM Identity Center instance `ssoins-7223e3853dd125e6`;
- Identity Store `d-9066776c6e`;
- permission set `AdministratorAccess` (`ps-722337dc1411b521`) с session duration 12 часов;
- group `WorkflowControlCenterDevelopmentAdministrators`, содержащая user `a.antonov` и назначенная только этому account;
- local AWS CLI profile `workflow-control-center-devel`.

Account остаётся под `SCP` `SandboxGuardrails`. Organization membership, OU placement, Identity Center group membership и account assignment являются внешним состоянием, не моделируемым CloudFormation, и проверяются по фактическому AWS state. Production account `227373271916`, account `o-petrakov-sandbox` и инфраструктура маркетплейсов не используются для разработки или приёмки WCC.

Человеческий доступ использует IAM Identity Center group `WorkflowControlCenterDevelopmentAdministrators` и существующий permission set `AdministratorAccess`. Static AWS access keys запрещены. Пользователь предоставил постоянное разрешение выполнять любые необходимые изменения в account `463564115167` без отдельной паузы; существенные изменения перечисляются при handoff. Это разрешение не распространяется на другой аккаунт.

Platform role `workflow-control-center-platform` использует AWS managed `AdministratorAccess` без ограничений по service, resource name, tag или ARN prefix. Такая permission policy совпадает с будущей выделенной production platform role; между средами различаются trust и physical account identity. Delegated Data/Athena roles остаются пользовательской least-privilege boundary и не раскрывают platform credentials.

Root credentials дочернего account не создаются и не восстанавливаются. Перед AWS mutation profile обязан успешно выполнить STS identity и read-only service checks S3, KMS, Athena и CloudFormation. `NotSignedUp`, `SubscriptionRequiredException` или `OptInRequired` являются account readiness failure и не разрешают ослабить IAM/SCP или обойти gate.

## Стеки И Владение

Существующий stack `workflow-control-center-development` остаётся стабильным data-plane stack. При переносе шаблона из `marketplace-infrastructure` сохраняются его имя, параметры, outputs, logical resource IDs и physical resources. Он владеет:

- приватными versioned buckets `Data`, `Secret`, `Result` и `Observability`;
- общим customer-managed key KMS среды разработки и bucket policies;
- platform role, Data credential roles, Athena query roles и Lake Formation data-access role;
- Glue database `workflow_data_000`, исходными isolation tables, Lake Formation registration и grants;
- Athena workgroup с scan cutoff `100 MiB` на query и CloudWatch alarms;
- региональным API Gateway endpoint контролируемой проверки VPN;
- исходными outputs для Product runtime и real AWS acceptance.

Новый stack `workflow-control-center-development-compute` владеет VPC, subnet, routing, security group, instance profile, EC2 instance, retained EBS volume, snapshot automation, Session Manager lifecycle и внешним stop lease. Compute replacement не изменяет data-plane resources.

CloudFormation связывает EC2 instance с `DevelopmentLaunchTemplate.LatestVersionNumber`, а запущенный EC2 instance всегда фиксирует использованную concrete immutable version в AWS launch-template metadata. Ordinary `apply` отклоняет change set с `Remove`, `Conditional` или `True` replacement для instance, retained volume или attachment, поэтому изменение launch-template data, которое затрагивает instance, выполняется только через explicit `replace`/`restore`. Эта операция переключает alternating slot, включает replacement guard, доказывает остановку старого instance, отсоединяет retained EBS и только после этого разрешает CloudFormation создать новую version и replacement instance. После update orchestrator доказывает, что version в EC2 metadata точно равна stack output `LatestLaunchTemplateVersion`.

При неуспешном replacement stack обязан завершить rollback, после чего orchestrator повторно подключает stack-declared retained volume к восстановленному остановленному instance и доказывает exact attachment до возврата ошибки. CloudFormation replacement не используется как механизм одновременного подключения одного EBS к старому и новому instance.

Отдельный stack AWS Budget в management account `227373271916` удаляется в рамках утверждённого cutover. Новый AWS Budget не создаётся.

Все CloudFormation resources разработки используют tags `Project=workflow-control-center`, `Environment=development` и `ManagedBy=CloudFormation`, когда resource type поддерживает tags. Stacks development account не изменяют production или management-account resources, кроме отдельно утверждённого удаления obsolete Budget.

## Физический Data Plane

Buckets используют stack-owned transparent names с account ID, Block Public Access, bucket-owner-enforced ownership, TLS-only policy, versioning, default SSE-KMS общим development key и S3 Bucket Keys. Их lifecycle реализует точные Product contracts из `workflow-control-center` — `design/data-storage.md`, включая abort incomplete multipart через один день, history Data/Secret 30 дней, `data-download` два дня и Observability source maps 30 дней.

Exact Observability bucket name — `workflow-control-center-devel-463564115167-observability`, а source-map prefix — `source-map/workflow-control-center-ui@<git-sha>/**`. Bucket не хранит GlitchTip events/database или Product Data. Только platform role управляет artifacts; GlitchTip AWS credentials не получает.

API Gateway публикует только `GET /development/ip` для controlled VPN validation. Response содержит source IP, `Content-Type: application/json` и `Cache-Control: no-store`; stage не включает execution logs, data tracing, detailed metrics или cache и использует rate `10 requests/second`, burst `20`.

Все users используют shared Glue database `workflow_data_000`, а physical table names начинаются с `user_<UserDataRoot.id hex>__`. Query role требует session tag `UserDataRootId`; IAM table prefix и Lake Formation table wildcard должны разрешить доступ одновременно. Data location использует dedicated Lake Formation access role и status `VERIFIED`. Каждая table location является exact tenant root, а symlink manifests и targets остаются ниже того же root. Platform role имеет явный `ALL` с grant option на database и table wildcard для reconciliation; tenant query role не получает этот control-plane access.

Stack outputs являются единственным infrastructure input Product runtime и AWS acceptance. Они включают account/region, platform role, Data/Secret/Result/Observability buckets, KMS key, Glue database, query role, Athena workgroup, VPN validation URL и fixed two-tenant acceptance identities/tables. Runtime не выводит physical resource names из local defaults.

## Вычислительный Узел

Среда использует один EC2 instance `m7g.xlarge` с Ubuntu Server 24.04 LTS и одним single-node k3s cluster. Canonical AMI ID разрешается при apply через публичный параметр SSM Canonical для Ubuntu 24.04, выбранный по `ComputeArchitecture`; AMI ID не закрепляется вручную.

Направление увеличения размера определяется фактическим ограничением:

- при ограничении одного ядра или I/O: `m7g.xlarge -> m8g.xlarge -> m9g.xlarge`;
- при ограничении CPU параллельными сборками: `c6g.2xlarge -> c7g.2xlarge -> c8g.2xlarge -> c9g.2xlarge`;
- при нехватке памяти: `r7g.xlarge -> r8g.xlarge ->` следующая доступная `r*.xlarge`.

Изменение семейства не меняет runtime platform скрыто: новый deployment заново определяет platform, проверяет совместимость и создаёт новый immutable release.

## Сеть И Доступ

Compute stack создаёт отдельную VPC, одну public subnet, Internet Gateway и route к нему. NAT Gateway и платные interface VPC endpoints отсутствуют. Бесплатный S3 gateway endpoint допустим. Instance получает временный public address без Elastic IP; operator и automation адресуют его по instance ID через Systems Manager.

Security group не имеет входящих правил для `22`, `80` или `443`. Обычная консоль работает через Session Manager, SSH/SCP/rsync/Remote SSH — через SSH-over-SSM, а Product HTTP — через один SSM port-forwarding session. Локальный вход Product использует `http://localhost:8080`; виртуальные hosts ZITADEL и GlitchTip используют соответствующие `*.localhost:8080` origins. Внешний публичный Product endpoint отсутствует.

Длительные host-control операции через SSM Run Command ожидаются по фактическому
invocation status, а не по короткому default waiter AWS CLI. Локальный orchestrator
допускает до одного часа на завершение одной операции восстановления или установки,
показывает точный command ID при timeout и не отменяет продолжающуюся удалённую
операцию автоматически.

IMDSv2 обязателен, hop limit равен `1`. Kubernetes workloads дополнительно не имеют маршрута к `169.254.169.254`. NetworkPolicy явно разделяет Product, workflow, browser, VPN, database, observability, registry и builder boundaries.

## Диски И Постоянное Состояние

Заменяемый root/scratch volume — encrypted gp3 `100 GiB`. Он хранит OS, k3s datastore, containerd, Docker/BuildKit cache, `emptyDir`, текущую копию infrastructure control source и host service definitions; удаляется при замене instance и не получает snapshots. Exact Product source releases и их deployment artifacts на root volume не хранятся.

Отдельный encrypted gp3 volume `80 GiB` хранит:

```text
/srv/workflow-control-center/
  glitchtip/
  observability/
  postgres/
  release/
    current -> releases/<release>/
    releases/
  secrets/
  workflow-registry/
  workflow-run/
```

`release/releases/<release>/` содержит exact source graph, source manifest, Product release manifest, Kustomize render, release-local ingress manifest и Helm archives. `release/current` является абсолютной атомарно заменяемой ссылкой только на полностью принятый child release. Root-volume `/opt/workflow-infrastructure/current` является восстановимой ссылкой на retained `release/current`, а не владельцем Product release.

Volume имеет `DeletionPolicy: Retain` и `UpdateReplacePolicy: Retain`. Ordinary
stop/start сохраняет его. Instance replacement повторно подключает тот же volume.
Семь ежедневных incremental snapshots обеспечивают восстановление на новый volume,
включая exact current Product release, необходимый для пересоздания disposable k3s.

`AWS::EC2::Volume.SnapshotId` нельзя обновить у существующего physical volume.
Поэтому stack имеет base retained resource и два alternating restore resources
`a`/`b`. Обычный replacement сохраняет текущий retained-volume slot, а каждый
snapshot restore выбирает следующий slot, создаёт новый physical volume и оставляет
предыдущий volume по `Retain`. До остановки compute orchestrator доказывает, что
snapshot принадлежит development account, завершён, зашифрован и помещается в
утверждённый volume. После update он доказывает новый physical ID, exact
`SnapshotId`, encryption и attachment, а затем исключает оставленный старый volume
из DLM target tag. Поэтому daily lifecycle продолжает обслуживать только current
retained volume. Cost boundary разрешает одновременно только current volume и один
предыдущий rollback volume: перед следующим restore orchestrator удаляет более
старый volume только после доказательства exact stack ownership, encryption,
одинакового размера и KMS key, состояния `available`, отсутствия attachments и DLM
backup tag. Текущий volume никогда не входит в cleanup. В результате restore
сохраняет одну непосредственную точку ручного rollback без неограниченного gp3 и
snapshot fan-out.

DLM schedule использует `CopyTags=false` и задаёт snapshot-owned `Name`,
`Project`, `Environment` и `ManagedBy=DLM` через `TagsToAdd`; volume-owned
CloudFormation и selection tags не копируются на snapshot и не создают duplicate
keys. Приёмка проверяет не только CloudFormation status и drift, но и provider
state exact policy: `ENABLED` обязателен, а `ERROR` является отказом apply и явно
виден в `status`.

`postgres/` физически объединяет БД `apwid`, `apwid_test`, `zitadel` и `glitchtip`, но их логические lifecycle различаются. Product reset может пересоздать только `apwid`, `apwid_test`, workflow registry, WorkflowRun storage и явно выбранные Product data-plane objects. Он сохраняет ZITADEL users, password hashes, identity-provider links, Product role grants, GlitchTip database, uploaded files и source-map state.

## k3s И Bootstrap

k3s version закрепляется явно. Single-node server включает secrets encryption, не публикует Kubernetes API наружу и не использует встроенный Traefik. Ingress-nginx и все Product manifests отслеживаются проектом `workflow-control-center`.

Cloud-init/UserData выполняет только:

- форматирование и mount retained volume;
- установку минимальных base packages без `apt upgrade`;
- загрузку `install.sh` из exact k3s release tag, проверку declared SHA-256 и установку той же закреплённой k3s version;
- установку Docker/Buildx и настройку SSM/systemd substrate.

UserData не содержит infrastructure/Product source, Product secrets, deployment logic или GitHub credentials и потому не может владеть host lifecycle controller. Create/replacement flow ждёт успешный cloud-init, через SSM публикует exact clean infrastructure control source на disposable root, устанавливает controller из его воспроизводимого Python 3.14 virtual environment и доказывает mount, k3s, node и controller readiness до отключения replacement guard. Обычный stop/start переиспользует уже установленный controller и не выполняет source deploy.

Перед каждым Product deploy exact переданный source release выполняет source-owned host preparation. Он атомарно устанавливает только закреплённый Helm release для фактической host architecture после проверки declared SHA-256; отсутствие или другая версия Helm не компенсируются package-manager latest, непроверенным install script или состоянием workstation. Этот шаг выполняется до Product image/chart path и повторяется без изменений при замене instance.

## AWS Credentials В Runtime

EC2 получает platform role через instance profile. EC2 temporary credentials автоматически обновляются AWS и являются единственным host credential source.

Product-owned host credential refresher устанавливается из exact current WCC release как systemd oneshot/timer, использует `aws configure export-credentials --format process` и атомарно обновляет узко распространяемый Kubernetes `Secret` со стандартным `credential_process` JSON. Backend, platform workers, run-local control services и bounded trusted materialization Jobs монтируют JSON read-only и используют стандартную AWS SDK credential chain, которая обновляет session до expiry без Pod restart или ручного apply. Их readiness проверяет timezone-aware expiration и workload health без вывода credential fields. AWS access key, secret и token не передаются через environment variables или command arguments.

Workflow, browser, VPN provider, WorkflowSource candidate test и credentialless wait Pods никогда не получают platform credentials. Trusted materializer передаёт им только exact snapshot и atomic readiness marker через private run/attempt volume. Temporary AWS credentials не входят в retained secret archive, release manifest или logs. Истечение credential без успешного refresh закрывает AWS-dependent operations; static key, anonymous access и local S3 fallback отсутствуют. Истечение laptop SSO session не останавливает уже работающий Product.

## Доставка Исходников И Release

Локальный orchestrator принимает только чистые exact checkouts требуемых репозиториев. Для каждого source он проверяет remote URL, commit SHA, отсутствие untracked/modified files и опубликованность exact commit в upstream. Исходники передаются через rsync поверх SSH-over-SSM; EC2 не хранит GitHub credentials.

Передаваемый набор содержит только tracked required files. Orchestrator создаёт content manifest, проверяет его на host и помещает exact source release на retained volume. Product current links переключаются атомарно только после полной Product acceptance. Release manifest сохраняет repository URLs, source commit SHAs, archive/content digests, runtime platform, immutable image digests, exact Helm chart versions/archive digests, release-local ingress-nginx source URL/digest, digest применённого Kubernetes render и timestamp deployment.

Platform images собираются на EC2 через Docker Buildx для exact target platform и публикуются в persistent cluster-local OCI registry по immutable digest. User `WorkflowSourceVersion` images продолжают собираться cluster-local rootless BuildKit и принимаются только после platform и publisher validation. Runtime registry и accepted workflow images находятся на retained volume.

Deployment сначала сохраняет versioned ingress-nginx manifest и exact Helm chart archives внутри immutable release, затем собирает все images и source-map artifacts и фиксирует exact image digests в одном Kustomize render. Install не применяет remote URL или mutable chart reference напрямую. Release становится active только после rollout readiness, Product smoke и exact current-assets verification. При failure восстанавливаются previous Helm revisions, previous release-local ingress manifest и previous exact render; частично собранный набор не становится current.

Replacement или snapshot recovery сначала публикует на disposable root только текущий trusted infrastructure control source. Этот control source проверяет retained current link, оба release manifests, exact source/render digests и каждый tracked source byte до восстановления `/opt/workflow-infrastructure/current`. После этого Product-owned `recover`:

- принимает release identity и target platform только из сохранённого manifest;
- требует уже сохранённые Helm archives и ingress manifest и никогда не скачивает им замену;
- требует все exact image digests в retained registry;
- восстанавливает retained PostgreSQL, secrets, ZITADEL, GlitchTip и source maps;
- применяет сохранённый exact render и переустанавливает renewable credential service;
- не собирает Product images, не создаёт новый release и не переписывает release manifest.

Таким образом новый disposable k3s восстанавливается из snapshot-owned state с той же release identity. Обычный новый deploy после replacement не является recovery и не может подменять этот контракт.

## Платформа Runtime

Deployment получает `(operatingSystem, architecture)` от всех Kubernetes nodes, которым разрешено выполнять `WorkflowRun`. Все eligible nodes обязаны сообщить одну одинаковую platform; смешанный набор отклоняется. OS обязана быть Linux из-за `SIGSTOP`, process namespaces, Unix sockets и `fsGroup`.

Node platform нормализуется в OCI form и явно передаётся всем Docker/BuildKit builds. Builder process architecture не является источником target. `BUILDPLATFORM`, `TARGETPLATFORM`, `TARGETOS` и `TARGETARCH` используются как стандартные BuildKit inputs, а accepted OCI manifest/config обязаны совпадать с target.

Target platform является частью immutable build attempt и release identity. Смена architecture не переписывает существующие images. В pre-production она требует чистого Product reset и новых build/version identities; multi-architecture publication остаётся отдельным будущим design.

`browser-runtime` использует bundled Playwright Chromium, доступный для target platform, и не зависит от установленного Google Chrome или Playwright channel `chrome`.

## Автоматическая Остановка

Early stop разрешён после непрерывных 30 минут, в течение которых:

- нет connected Session Manager sessions для instance;
- WCC activity probe доказывает отсутствие незавершённых WorkflowRun, builds, VPN validations, Data/Athena operations, recovery и обязательного cleanup.

CPU, load average и network counters не участвуют в решении. Ошибка probe, AWS API или controller считается `busy`. Перед stop controller повторно проверяет условия, cordon-ит node, выполняет graceful drain, останавливает Product containers и k3s и вызывает poweroff. Failure снимает cordon и оставляет instance работающим.

Перед каждым explicit `StartInstances` внешний controller создаёт renewable one-time EventBridge Scheduler action на `now + 2 hours`. Target является стабильной stack-owned Lambda, которая при expiry находит все running `DevelopmentInstance` этого exact CloudFormation stack по системным и project tags и вызывает `StopInstances`. Lambda не получает Product credentials или данные и имеет только tag-limited `StopInstances`, read-only instance discovery и собственные logs.

CloudFormation create/replacement защищает отдельный stack-owned `ReplacementGuardSchedule`. В steady state он `DISABLED`; explicit create/replace/restore передаёт exact двухчасовой expression и `ENABLED`, а `DevelopmentInstance` зависит от этого schedule. Поэтому guard создан или обновлён до запуска instance, даже когда будущий EC2 instance ID неизвестен или renewable target появляется в этом же первом stack update. После readiness и доказанного renewable lease orchestrator отдельным identity-preserving change set переводит guard в `DISABLED`. Если orchestrator исчезает во время replacement, guard остаётся включённым и останавливает созданный instance.

Host обновляет lease каждые 30 минут, пока доказывает активную SSM session или полезную WCC работу. Healthy длительный run не имеет hard deadline и продолжает renew. Если host или controller перестал renew, AWS останавливает instance не позднее двух часов после последнего успешного lease.

Scheduler action использует `ActionAfterCompletion=DELETE`. Ordinary stop удаляет pending schedule. Если initial schedule не создан, instance не запускается.

## Контроль Стоимости

Утверждённая архитектура этого документа является текущим cost checkpoint. Для следующего изменения сравниваются checkpoint и proposal по одинаковым текущим ценам и одинаковым usage assumptions, чтобы выделить только архитектурный delta.

Net projected recurring monthly delta накапливается от последнего явного одобрения:

- `<= USD 10/month` можно применять без отдельного согласования;
- `> USD 10/month` требует согласования до apply;
- cost reductions компенсируют increases;
- после явного согласования proposal становится новым checkpoint;
- неограничиваемая или недоказуемая стоимость требует согласования.

Одноразовый projected spend свыше `USD 10` согласуется отдельно. Границы production, account ownership и security действуют независимо от стоимости. Auto-stop и renewable lease являются lifecycle architecture, а не substitute AWS Budget.

Максимальный fixed gp3 checkpoint равен `260 GiB`: `100 GiB` root/scratch,
`80 GiB` current retained volume и не более одного `80 GiB` предыдущего rollback
volume. Семь daily snapshots создаются только для current retained volume.
Консервативная верхняя граница их billed snapshot storage равна
`7 × 80 GiB = 560 GiB`: incremental block reuse может уменьшить фактический объём,
но не используется как недоказанное условие расчёта максимума.

Перед compute apply cost review получает из AWS Price List API текущие exact
regional price dimensions для S3 Standard storage и requests, customer-managed
KMS keys и requests, Glue Data Catalog storage и requests, Athena scanned data,
REST API Gateway requests и Internet data transfer. Для tiered meters сохраняются
все диапазоны, а не один выбранный rate. Их утверждённый usage baseline не
превращается в выдуманное фиксированное потребление: записывается, что quantity
архитектурного delta равна нулю, пока соответствующий contract не изменён.

## Разрушающий Cutover

Переход с laptop kind на EC2 k3s выполняется до production и не поддерживает параллельные local/remote deployment branches. После полной remote acceptance local-kind tooling, overlays и operational path удаляются.

Состояние переносится избирательно:

- ZITADEL и GlitchTip сохраняются через logical PostgreSQL dump/restore, long-lived runtime secrets, GlitchTip files и source-map artifacts;
- БД `apwid` и `apwid_test`, workflow registry и WorkflowRun storage создаются заново;
- все development Data, Secret и Result object versions и dynamic Glue/Athena catalog полностью очищаются и bootstrap-ятся заново;
- Product, browser, VPN и WorkflowSource images собираются заново для target platform.

Полный raw copy laptop cluster или OCI registry запрещён, потому что существующие accepted images могут иметь несовместимую architecture. Старое локальное состояние сохраняется на laptop до полной remote acceptance и удаляется только по отдельному явному запросу пользователя.

Database migration, compatibility bridge, dual deployment и old/new state synchronization отсутствуют. Logical preservation ZITADEL/GlitchTip является операционным переносом обязательного состояния, а не product migration framework.

## Восстановление

Acceptance обязана доказать три независимых сценария:

1. stop/start того же instance с тем же retained volume;
2. replacement instance с повторным подключением того же retained volume;
3. новый retained volume, восстановленный из выбранного snapshot.

Каждый сценарий проверяет PostgreSQL, ZITADEL user и grants, GlitchTip state, registry, незавершённый WorkflowRun/recovery contract, AWS data plane, Product UI и exact release identity. Recovery не считается успешным по одному mount, Pod readiness или stack status.

## Запрещённые Обходы

- WCC infrastructure, шаблоны или forwarding docs в `marketplace-infrastructure`.
- Второй local-kind deployment path после remote acceptance.
- Входящие SSH/HTTP rules, Elastic IP или persistent GitHub credentials.
- Static AWS keys, AWS credentials в environment variables Product Pods или platform credentials в workflow/browser/VPN Pods.
- NAT Gateway или paid interface endpoints без отдельного cost approval.
- Product source, secrets или deployment policy в UserData.
- Builder-default platform, hardcoded `linux/arm64` или silent image rewrite при смене architecture.
- CPU/load/network-based idle decision или абсолютный max uptime здорового instance.
- Raw copy несовместимого local registry либо потеря ZITADEL/GlitchTip при Product reset.

## Проверки

CloudFormation acceptance доказывает exact account/region, stack ownership, no replacement stable data-plane resources, retained policies, IAM trust, VPC routes, отсутствие ingress, IMDSv2, Scheduler lease и семь snapshots. Cost review использует текущие цены и явно записанные usage assumptions.

Deployment acceptance доказывает clean exact source manifests, native target platform, immutable image digests, registry persistence, rollback previous release, Product readiness, source-map publication и отсутствие credentials в source, logs, images и retained archives.

Operational acceptance доказывает SSM console, port forwarding, SSH-over-SSM, 30-minute idle stop, useful-work lease renewal beyond two hours, fail-safe stop after renewal loss и все три recovery scenarios. Product acceptance следует `workflow-control-center` и выполняется через current deployed assets по SSM tunnel.

Provider-independent lifecycle policy проверяется controlled clock. Отдельная
operator-only `lifecycle-acceptance` использует фиксированную сокращённую
конфигурацию только для реального AWS перехода create-renew-expire-delete,
останавливает production controller на время проверки и всегда восстанавливает
обычные интервалы, controller и Product readiness. Сокращённые интервалы не являются
runtime configuration production service.
