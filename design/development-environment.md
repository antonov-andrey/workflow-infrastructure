# Среда Разработки `Workflow Control Center`

## Назначение

`design/development-environment.md` владеет только development-специализацией общей модели из `design/environment-model.md`: AWS-аккаунтом разработки, стеками `CloudFormation`, одним вычислительным узлом, k3s, постоянным состоянием, доступом оператора, доставкой release, AWS credentials, автоматической остановкой, восстановлением и контролем стоимости. Product contracts данных и runtime принадлежат `workflow-control-center`, а production adapters — `design/production-environment.md`.

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
- existing organization-level IAM Identity Center group `WorkflowControlCenterDevelopmentAdministrators`, содержащая user `a.antonov` и назначенная только этому account;
- local AWS CLI profile `workflow-control-center-devel`.

Account остаётся под `SCP` `SandboxGuardrails`. Organization membership, OU placement, Identity Center group membership и account assignment являются внешним состоянием, не моделируемым CloudFormation, и проверяются по фактическому AWS state. Account `227373271916` является Organizations management account, а не production account WCC. Будущий production account ещё не назначен. Management account, account `o-petrakov-sandbox` и инфраструктура маркетплейсов не используются для разработки или приёмки WCC.

Человеческий доступ использует existing organization-level IAM Identity Center group `WorkflowControlCenterDevelopmentAdministrators` и существующий permission set `AdministratorAccess`. Эта внешняя identity subject не является account-local resource identity, не создаётся и не переименовывается environment stacks и не ослабляет запрет project name внутри development-account resource names/tags. Static AWS access keys запрещены. Пользователь предоставил постоянное разрешение выполнять любые необходимые изменения в account `463564115167` без отдельной паузы; существенные изменения перечисляются при handoff. Это разрешение не распространяется на другой аккаунт.

Environment platform role (`platform-primary` либо `platform-<task-environment-name>`) использует AWS managed `AdministratorAccess` без ограничений по service, resource name, tag или ARN prefix. Это намеренное исключение только для полностью выделенного development account, а не шаблон production workload permissions. Production использует отдельные least-privilege EKS Pod Identity roles. Delegated Data/Athena roles разработки остаются пользовательской least-privilege boundary и не раскрывают platform credentials.

Root credentials дочернего account не создаются и не восстанавливаются. Перед AWS mutation profile обязан успешно выполнить STS identity и read-only service checks S3, KMS, Athena и CloudFormation. `NotSignedUp`, `SubscriptionRequiredException` или `OptInRequired` являются account readiness failure и не разрешают ослабить IAM/SCP или обойти gate.

## Идентичность Development Environment

Development environment имеет stable name. Default name `primary` предназначен для основной длительной среды. Task environment принадлежит exact goal-brainstorm common prefix и получает machine name `w` плюс первые 15 lowercase hexadecimal symbols SHA-256 этого prefix. Controller до provisioning сравнивает persisted full prefix; collision одного machine name с другим prefix fail closed. Одна task environment общая для всех participating repository worktrees одной task pair и получает отдельный data plane, compute, retained volume, registry, Product database, release pointer, credentials, SSM tunnel и lifecycle controller; скрытое разделение mutable cluster или Product state между задачами запрещено.

Task environment создаётся lazy первым `apply`/`deploy`, использует ordinary auto-stop и существует до explicit synchronized goal cleanup. Task checkout обязан передать exact `--git-worktree <common-prefix>` и никогда не fallback-ится на `primary`.

## Стеки И Владение

Stack `account-foundation` является единственным account-global owner. Он владеет account-level S3 Block Public Access, Lake Formation account settings, KMS-encrypted CloudWatch log group and `SSM-SessionManagerRunShell` preferences, а также primary-only AWS Backup plan/selection. Session preferences требуют запись ordinary shell command/output только в этот encrypted log group; `kmsKeyId`, который отдельно включает KMS-шифрование интерактивного client-to-instance channel и требует другого IAM-контракта, не подменяет log encryption и здесь не задаётся. Initial foundation apply создаёт guards, logging, vault/plan/role без selection. После создания `data-primary` тот же owner добавляет exact `platform-primary` в `DataLakeSettings.Admins` только проверенным переходом пустого `PrimaryPlatformRoleArn` к ARN роли текущего account. После создания primary retained volume он получает exact volume ARN и reconciles единственную selection. Task apply только проверяет accepted foundation и не изменяет administrator list либо backup selection. Environment stacks до и после apply проверяют exact live state и никогда не создают competing owner.

Stack `data-<environment-name>` владеет:

- приватными versioned buckets `Data`, `Secret`, `Result` и `Observability`;
- общим customer-managed key KMS среды разработки и bucket policies;
- platform role, Data credential roles, Athena query roles и Lake Formation data-access role;
- environment-local Glue database zero (`data_000_primary` либо `data_000_<task-environment-name>`), исходными isolation tables, Lake Formation registration и grants;
- Athena workgroup с scan cutoff `100 MiB` на query и CloudWatch alarms;
- private `vpn-validation/` prefix and one-day lifecycle in the Observability bucket для one-attempt VPN validation nonces;
- исходными outputs для Product runtime и real AWS acceptance.

Stack `compute-<environment-name>` владеет VPC, subnet, routing, security group, instance profile, EC2 instance, retained EBS volume, Session Manager lifecycle и внешним stop lease. Compute replacement не изменяет data-plane resources. Старые project-prefixed stack/resource names являются pre-production state и уничтожаются вместо compatibility aliases; globally unique bucket names добавляют account ID и region.

### Границы Operator-Модулей

`workflow_infrastructure/development_environment/composition.py` является только composition root: он создаёт и связывает capability owners, а корневой Product entrypoint `development_environment_manage.py` обращается к соответствующему owner напрямую. Provisioning, lifecycle, replacement, release и diagnostics не образуют второй скрытый orchestration слой внутри composition root. Общий development-environment context принадлежит пакету `workflow_infrastructure/development_environment/`; host-specific owners находятся в `host/`, а Product release owners — в `product/`. Плоский набор `development_*.py` в общем каталоге запрещён. Конкретные границы имеют единственных владельцев:

| Владелец | Ответственность |
| --- | --- |
| `account.py` | caller/account identity и проверка единственного account-foundation owner |
| `access.py` | интерактивные Session Manager tunnel, console и SSH-over-SSM process lifecycles |
| `aws.py` | единый AWS CLI transport |
| `compute.py` | EC2 identity/state, launch-template proof, SSM readiness и runtime platform facts |
| `cost.py` | cost checkpoint и pricing review |
| `diagnostics.py` | безопасная агрегация operator status и bounded diagnostic commands |
| `identity.py` | единое выведение environment-local AWS и host identities |
| `command.py` / `clock.py` | явные process и time boundaries composition graph |
| `lifecycle.py` | EC2 start/graceful stop и real stop-lease acceptance |
| `provisioning.py` | полный apply двух CloudFormation stacks и post-apply acceptance |
| `replacement.py` | replacement/restore cutover, alternating slots и fail-safe guard |
| `stack.py` | CloudFormation template transport, change sets, apply и drift |
| `retained_volume.py` | retained EBS, restore slots, temporary snapshots и primary-only AWS Backup proof |
| `source.py` | exact source resolution, archive и delivery |
| `transport.py` | SSM Run Command и SSH-over-SSM |
| `host/manager.py` | host-local controller port, activity proof, dependency validation, service install и shutdown |
| `host/status.py` | локальный и SSM-сбор нормализованного safe host status |
| `host/artifact/model.py` | immutable host-artifact models and resolved identities |
| `host/artifact/download.py` | shared byte download/cache boundary without provider policy |
| `host/artifact/git_ref.py` | exact Git ref resolution and export |
| `host/artifact/verification.py` | trust, checksum and signature verification |
| `host/artifact/provider/aws_cli.py` | AWS CLI resolution/install contract |
| `host/artifact/provider/docker.py` | signed Docker package-graph resolution/install contract |
| `host/artifact/provider/python.py` | uv/Python resolution/install contract |
| `host/artifact/provider/k3s.py` | k3s resolution/install contract |
| `host/artifact/provider/helm.py` | Helm resolution/install contract |
| `host/artifact/resolver.py` | provider dispatch and explicit dependency wiring only |
| `host/bootstrap/artifacts.py` | verified bootstrap-bundle artifact exposure |
| `host/bootstrap/storage.py` | retained/root volume initialization and mounts |
| `host/storage/initialization.py` | control-plane authorization and monotonic acceptance of first-time retained XFS creation |
| `host/bootstrap/network.py` | host firewall and network hardening |
| `host/bootstrap/k3s.py` | k3s installation/configuration readiness |
| `host/bootstrap/services.py` | Product-owned systemd service installation and readiness; SSM agent installation/reconfiguration запрещены |
| `host/bootstrap/manager.py` | idempotent bootstrap sequence and wiring only |
| `product/deployment.py` | immutable Product source publication, deploy, activation и service install |
| `product/recovery.py` | durable Product recovery transitions и acceptance |
| `product/public_ecr_auth.py` | release-local AWS Public ECR login и гарантированное удаление Docker credential directory |
| `product/release/tool.py` | exact current Product tool path and invocation, без release lifecycle policy |
| `product/release/manager.py` | host-only retained release transition sequence and dependency wiring |
| `product/release/manifest.py` / `recovery_contract.py` | host artifact and complete retained release validation |
| `product/release/recovery.py` / `rollback.py` / `reset.py` | durable recovery marker, current/rollback pointers and destructive reset ownership |
| `storage.py` | idle lifecycle, maintenance cadence и volume-pressure warnings |

Новая независимая provider-, storage-, security-, artifact- или persisted-state семья добавляется отдельным collaborator в той же change, где она появляется. Перенос методов в mixin, pass-through facade либо файл без переноса state и инвариантов не считается разделением ответственности.

CloudFormation связывает EC2 instance с конкретным числовым `InstanceLaunchTemplateVersion`, а запущенный EC2 instance всегда фиксирует использованную concrete immutable version в AWS launch-template metadata. Ordinary compute phase создаёт новую immutable version, сохраняя текущий instance на его active version; обнаруженное различие `active != latest` переводит тот же `apply` в controlled replacement. Explicit `replace`/`restore` используют тот же replacement primitive как самостоятельные operator recovery commands. Каждая replacement operation переключает alternating slot, включает replacement guard, доказывает остановку старого instance, отсоединяет retained EBS и только после этого разрешает CloudFormation заменить instance. После update orchestrator доказывает, что version в EC2 metadata точно равна stack output `LatestLaunchTemplateVersion`.

Теги EC2 instance принадлежат непосредственно `DevelopmentInstance.Properties.Tags`; launch template владеет только тегами создаваемого disposable root volume. Instance-теги через `LaunchTemplateData.TagSpecifications` запрещены: AWS применяет их физически, но CloudFormation не включает их в expected properties `AWS::EC2::Instance`, создавая постоянный ложный drift. Любой фактический drift закрывает операцию; pre-hardening templates, transitional tags и отдельные reconcile-исключения не поддерживаются.

Незавершённый replacement с включённым guard возобновляется по доказанному состоянию host. Успешный cloud-init продолжает source install и retained Product recovery. Terminal `cloud-init status: error` разрешает заменить disposable host только если exact retained root ещё не смонтирован и k3s не active; guard остаётся включённым, template сначала обязан предоставить более новую immutable launch-template version, а затем используется обычный controlled replacement. Неоднозначный cloud-init state, mounted retained root, active k3s или отсутствие более новой version закрывают автоматическое продолжение.

При неуспешном replacement stack обязан завершить rollback, после чего orchestrator повторно подключает stack-declared retained volume к восстановленному остановленному instance и доказывает exact attachment до возврата ошибки. CloudFormation replacement не используется как механизм одновременного подключения одного EBS к старому и новому instance.

Отдельный stack AWS Budget в management account `227373271916` удаляется в рамках утверждённого cutover. Новый AWS Budget не создаётся.

Taggable environment resources используют `EnvironmentClass=development`, `EnvironmentName=<stable environment name>` и `ManagedBy=CloudFormation`. Каждый task-specific resource дополнительно и точно имеет `git-worktree=<full common prefix>`. `primary` и `account-foundation` не имеют `git-worktree`. Project tag и project name в AWS account-local identifiers запрещены. Tag inventory является обязательным leak check, но deletion доказывается exact CloudFormation/resource inventory, а не blanket tag selection. Stacks development account не изменяют production или management-account resources, кроме отдельно утверждённого удаления obsolete Budget.

## Физический Data Plane

Buckets используют role/environment names с account ID и region, bucket-level Block Public Access, bucket-owner-enforced ownership, TLS-only policy, versioning, default SSE-KMS environment key и S3 Bucket Keys. Делегированные Data, query и Lake Formation роли могут использовать этот key только через regional S3 service и только с encryption context точного разрешённого bucket; пользовательская сессия не получает самостоятельный raw KMS decrypt boundary. Account-level S3 Block Public Access принадлежит только `account-foundation` и проверяется до/после apply любой environment. Environment stack не создаёт конкурирующие custom resource, Lambda, log group или IAM role. State-bearing Data, Secret и Result buckets и customer-managed KMS key используют `DeletionPolicy: Retain` и `UpdateReplacePolicy: Retain`; stack deletion не становится Product purge.

Data и Secret не имеют blanket `NoncurrentVersionExpiration`: reference-aware Product reconciler удаляет unreferenced versions через 24 часа и historical versions через 30 дней только после proof отсутствия retained reference. Lifecycle остаётся только у точных classes, где время является authoritative Product contract: incomplete multipart через один день, completed `data-download` через два дня, abandoned Athena result через 30 дней и Observability source maps через 30 дней.

Observability bucket использует globally unique форму `observability-<environment-name>-<account-id>-<region>`, а source-map prefix — `source-map/workflow-control-center-ui@<git-sha>/**`. Product name в source-map artifact identity является domain/release identity, а не AWS account resource prefix. Bucket не хранит GlitchTip events/database или Product Data. Только environment platform role управляет artifacts; GlitchTip AWS credentials не получает.

VPN validation не создаёт API Gateway или публичный endpoint. Только после выдачи slot trusted WCC controller пишет random nonce object в platform-only `vpn-validation/` prefix environment Observability bucket и создаёт presigned HTTPS GET URL на полный bounded scheduling/image-pull, validation, stop и transport budget. Data/Secret/Result tenant roles не имеют доступа к этому prefix. Signing session обязана жить дольше этого budget; иначе controller сначала обновляет renewable credentials. Credentialless Job получает URL mounted Secret file, выполняет один bounded lifecycle, читает exact nonce через SOCKS и не получает AWS identity. Controller удаляет object/Secret после result; one-day lifecycle удаляет abandoned nonce objects. Transient attempt освобождает slot до fair requeue.

URL lifetime не является отдельным deployment default: WCC вычисляет его для exact Version из полного Job deadline, termination grace и transport margin и подписывает именно на этот срок. Development temporary credential session обязана покрыть вычисленное значение; недостаточный остаток приводит к refresh до подписи либо fail-closed attempt без запуска credentialless Pod.

Все users одной environment используют её shared Glue database zero, а physical table names начинаются с `user_<UserDataRoot.id hex>__`. Stack output передаёт exact database-zero identity Product; Product сохраняет тот же environment suffix во всех следующих shards, а IAM query role имеет exact имя `query-<database_name>`. Ни allocator, ни reconciliation worker не выводят primary name из локального default при наличии другого output. Query role требует session tag `UserDataRootId`; IAM table prefix и Lake Formation table wildcard должны разрешить доступ одновременно. Permanent Data credential role дополнительно ограничивает S3 root через тот же mandatory principal/session tag, а выдаваемая session policy только сужает exact operation и path. Data location использует dedicated Lake Formation access role и status `VERIFIED`. Каждая table location является exact tenant root, а symlink manifests и targets остаются ниже того же root. Platform role имеет явный `ALL` с grant option на database и table wildcard для reconciliation; tenant query role не получает этот control-plane access.

Lake Formation account settings являются общей fail-closed guard, а не environment-local данными: они используют `Retain`, одинаковые defaults и принадлежат только `account-foundation`. Primary data stack создаёт `platform-primary`, после чего единственный foundation owner добавляет эту роль в global administrator list. Ни primary, ни task environment stack не владеют `DataLakeSettings`; task environment не меняет administrator list. Каждый environment stack явно выдаёт своей platform role `CREATE_DATABASE`, `DATA_LOCATION_ACCESS`, database `ALL` и table-wildcard `ALL` только в собственных границах. Поэтому task environment создаёт Glue shards и tenant query permissions, не переписывая global settings либо authority другой environment.

Foundation и environment stack outputs являются единственным infrastructure input Product runtime и AWS acceptance. Они включают account/region, foundation identity, environment platform role, Data/Secret/Result/Observability buckets, exact Observability `vpn-validation/` prefix, KMS key, Glue database, query role, Athena workgroup и fixed two-tenant acceptance identities/tables. Runtime не выводит physical resource names из local defaults.

CloudFormation template до `51,200` bytes передаётся inline. Более крупный template загружается тем же trusted operator в retained Observability bucket под content-addressed key `cloudformation-template/<environment_name>/<sha256>.yaml`: upload сохраняет SHA-256 checksum и metadata, последующий `HeadObject` доказывает exact length/checksum/identity, а `ValidateTemplate` и change set получают один private regional S3 URL. Объекты и их noncurrent versions автоматически удаляются S3 Lifecycle через 30 дней; CloudFormation уже сохраняет принятый template внутри stack. Если data-plane stack и его artifact bucket ещё не существуют, oversized template fail-closed вместо создания неуправляемого bucket.

## Вычислительный Узел

Среда использует один EC2 instance `m7g.xlarge` с Ubuntu Server 24.04 LTS и одним single-node k3s cluster. Canonical AMI ID разрешается при apply через публичный параметр SSM Canonical для Ubuntu 24.04, выбранный по `ComputeArchitecture`; AMI ID не закрепляется вручную.

Направление увеличения размера определяется фактическим ограничением:

- при ограничении одного ядра или I/O: `m7g.xlarge -> m8g.xlarge -> m9g.xlarge`;
- при ограничении CPU параллельными сборками: `c6g.2xlarge -> c7g.2xlarge -> c8g.2xlarge -> c9g.2xlarge`;
- при нехватке памяти: `r7g.xlarge -> r8g.xlarge ->` следующая доступная `r*.xlarge`.

Изменение семейства не меняет runtime platform скрыто: новый deployment заново определяет platform, проверяет совместимость и создаёт новый immutable release.

## Сеть И Доступ

Compute stack создаёт отдельную VPC, одну public subnet, Internet Gateway и route к нему. NAT Gateway и платные interface VPC endpoints отсутствуют. Бесплатный S3 gateway endpoint допустим. Instance получает временный public address без Elastic IP; operator и automation адресуют его по instance ID через Systems Manager.

Security group не имеет входящих правил для `22`, `80`, `443` или environment HTTP ports. Обычная консоль работает через Session Manager, SSH/SCP/rsync/Remote SSH — через SSH-over-SSM, а Product HTTP — через один SSM port-forwarding session. Primary использует `http://localhost:8080`; task environment получает deterministic port из полного common prefix. Data-plane stack сохраняет port отдельным `LocalHttpPort` parameter и атомарно резервирует account-global SSM Parameter `/development/http-port/<port>`; физическая уникальность имени отклоняет даже конкурентное создание colliding environment, а удаление stack освобождает reservation. Infrastructure передаёт этот port каждому WCC Product lifecycle command, а Product использует его как единую browser-facing и host-local identity для UI, ZITADEL, GlitchTip, backend issuer/Host, ingress hostPort, readiness и ingress CORS. Оба конца SSM tunnel используют один exact environment port; отдельной внутренней HTTP identity нет. Внешний публичный Product endpoint отсутствует.

Idle lifecycle использует только факт и время существования Session Manager session для exact instance. Content-level аудит port-forwarding и SSH-over-SSM не нужен для решения «instance использовался» и не заявляется: AWS не предоставляет session-content logging для этих tunnel types. Account-foundation создаёт encrypted CloudWatch log group с retention и account-level `SSM-SessionManagerRunShell` preferences, поэтому ordinary Session Manager shell сохраняет command/output. Эта audit policy не влияет на idle predicate, который использует только session metadata и Product activity.

Длительные host-control операции через SSM Run Command ожидаются по фактическому
invocation status, а не по короткому default waiter AWS CLI. Локальный orchestrator
допускает до одного часа на завершение одной операции восстановления или установки,
показывает точный command ID при timeout и не отменяет продолжающуюся удалённую
операцию автоматически.

IMDSv2 обязателен, hop limit равен `1`. k3s получает explicit Pod CIDR `10.42.0.0/16` и Service CIDR `10.43.0.0/16`; host firewall запрещает именно этому declared Pod CIDR маршрут к `169.254.169.254`, поэтому security boundary не зависит от изменяемого default k3s. В single-node development `NetworkPolicy` обязательно изолирует arbitrary publisher/runtime Pods, browser, VPN, validation, registry writer и builder proxy paths. Полная сегментация trusted Product, database и observability control plane является обязательным production adapter contract, но намеренно не имитируется вторым EKS-подобным контуром в development; здесь её внешние границы обеспечивают отсутствие ingress в VPC, SSM access, service-account policy, Secret distribution и IAM.

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

Volume имеет `DeletionPolicy: Retain` и `UpdateReplacePolicy: Retain`. Ordinary stop/start сохраняет его. Instance replacement повторно подключает тот же volume. Содержимое raw block device не используется как доказательство нового диска: AWS вправе представить логически пустой EBS нулевыми либо криптографически псевдослучайными блоками. Compute stack хранит единственное монотонное состояние `RetainedVolumeFilesystemState=pending|complete` и тот же `FilesystemState` tag. Только созданный без snapshot base volume начинается в `pending`; restored volume создаётся сразу как `complete`. Exact stack/volume/attachment/snapshot/tag proof разрешает host bootstrap создать XFS у `pending` volume, причём concurrent bootstrap сериализуется host lock. Распознанная не-XFS файловая система никогда не перезаписывается. После успешного XFS mount orchestrator отдельным защищённым compute change set переводит состояние в `complete`, не заменяя instance, volume или attachment. `complete` не возвращается в `pending`; отсутствие XFS у принятого volume останавливает bootstrap и никогда не даёт повторного `mkfs`. Только retained volume основной среды `primary` входит в ежедневный AWS Backup plan с хранением семи recovery points. Crash-consistent EBS recovery point достаточен для этой development-среды: это страховочная копия единственного тестового сервера, а не production contract application-consistent backup. Дополнительные development environments не получают регулярный backup plan и selection tag. Backup vault использует `RetainExceptOnCreate`: rollback его первого неудачного создания не оставляет неуправляемый пустой vault, а удаление уже принятого stack сохраняет vault.

`AWS::EC2::Volume.SnapshotId` нельзя обновить у существующего physical volume. Поэтому stack имеет base retained resource и два alternating restore resources `a`/`b`. Обычный replacement сохраняет текущий retained-volume slot, а каждый snapshot restore выбирает следующий slot, создаёт новый physical volume и оставляет предыдущий volume по `Retain`. До остановки compute orchestrator доказывает, что snapshot принадлежит development account, завершён, зашифрован и помещается в утверждённый volume. После update он доказывает новый physical ID, exact `SnapshotId`, encryption и attachment, а затем обновляет foundation-owned primary AWS Backup selection на exact ARN единственного current retained volume. Selection не полагается на tag-only discovery. Cost boundary разрешает одновременно только current volume и один предыдущий rollback volume: перед следующим restore orchestrator удаляет более старый volume только после доказательства exact stack ownership, encryption, одинакового размера и KMS key, состояния `available`, отсутствия attachments и regular-backup tag. Текущий volume никогда не входит в cleanup. В результате restore сохраняет одну непосредственную точку ручного rollback без неограниченного gp3 и snapshot fan-out.

AWS Backup plan, vault, service role и selection принадлежат только `account-foundation`; environment stacks их не создают. Selection использует exact ARN current retained volume `primary`; tag `regular-backup=primary` является проверяемой маркировкой, а не границей выбора. После restore tag остаётся только на current volume. Приёмка проверяет не только CloudFormation status и drift, но и фактические plan rule, vault, семидневный lifecycle, exact selection ARN и current-volume tag. Для task environment приёмка, наоборот, доказывает отсутствие selection и regular-backup tag.

Одноразовый EBS snapshot дополнительной environment допустим только как временный артефакт явного копирования диска. После успешного копирования оператор обязан удалить такой snapshot; он не включается в регулярную политику и не превращается в скрытую историю backups. Весь раздел задаёт только backup policy development ресурсов. Production backup/restore определяется отдельно в `design/production-environment.md`.

Host lifecycle каждую минуту наблюдает root и retained filesystems. `warning` возникает при `>=75%` used либо `<10 GiB` free, `critical` — при `>=90%` used либо `<5 GiB` free. Состояние сохраняется environment-local, в journal выводятся переходы, восстановление и не чаще одного напоминания за шесть часов; ошибка наблюдения не отключает fail-safe stop lease. Не чаще одного раза за шесть часов и только при доказанном idle host вызывает Product-owned retention maintenance. Точная reachability, registry read-only safepoint и deletion policy принадлежат WCC, а infrastructure владеет только cadence и безопасным вызовом current release.

`postgres/` физически объединяет БД `apwid`, `apwid_test`, `zitadel` и `glitchtip`, но их логические lifecycle различаются. Product reset может пересоздать только `apwid`, `apwid_test`, workflow registry, WorkflowRun storage и явно выбранные Product data-plane objects. Он сохраняет ZITADEL users, password hashes, identity-provider links, Product role grants, GlitchTip database, uploaded files и source-map state.

## k3s И Bootstrap

k3s version закрепляется явно. Single-node server включает secrets encryption, не публикует Kubernetes API наружу и не использует встроенный Traefik. Ingress-nginx и все Product manifests отслеживаются проектом `workflow-control-center`. Каждый Product Pod явно задаёт `serviceAccountName` и `automountServiceAccountToken`: token разрешён только workload’ам с доказанным Kubernetes API contract, а pinned third-party Helm charts проходят тот же fail-closed workload classification до установки.

Development adapter использует один retained cluster-local OCI registry. Registry Deployment имеет strategy `Recreate`, чтобы две replicas не писали один retained filesystem одновременно. Image pull выполняет node/containerd, поэтому writer-registry NetworkPolicy разрешает trusted node boundary, exact platform push/cleanup workload и один stateless read-only registry proxy, а не пытается авторизовать pull по namespace целевого Pod. Proxy пропускает только registry `GET`/`HEAD` и только exact optional platform-base repository; private user image repositories и write/delete API через него недоступны. Builder получает saved attempt digest через reserved `WORKFLOW_PLATFORM_BASE_IMAGE=<repository>@<digest>`, а BuildKit mirror направляет этот pull в proxy; mutable convenience tag не участвует в platform build identity. Builder экспортирует OCI artifact, который публикует trusted push boundary. Остальной cross-namespace access запрещён. Registry endpoints не входят в environment-neutral manifests и не становятся production contract.

При создании нового Product release официальный Docker Library mirror в AWS Public ECR авторизуется instance role через `GetAuthorizationToken`. Infrastructure создаёт отдельный root-only `DOCKER_CONFIG` под disposable environment state, передаёт его только Product deploy и удаляет в обязательном cleanup как после success, так и после failure. Token не попадает в аргументы процесса, source manifest, retained volume, journal либо operator output; постоянный `/root/.docker/config.json` не используется. Vendor images остаются в официальных vendor registries, а единственный официальный upstream image, публикуемый только в Docker Hub, разрешается там. Уже принятый release использует сохранённые digests и при recovery не требует upstream authentication.

Каждый Product deploy и retained recovery выполняет live registry gate до
принятия результата. Gate доказывает фактический kubelet pull exact
platform-base digest через read-only proxy, `GET`/`HEAD` того же manifest,
отказ private repository и mutation methods через proxy, trusted
build-worker write path и отсутствие writer-registry route у builder identity
и Pod из постороннего namespace. Проверка использует bounded credentialless
Jobs и всегда удаляет их; она не подменяется статическим чтением
`NetworkPolicy`.

Каждая user-source build attempt создаёт собственный disposable rootless BuildKit Pod по общему contract. Постоянный shared BuildKit Deployment/Service, межпользовательский mutable cache и повторное использование daemon между attempts отсутствуют.

Cloud-init/UserData только обеспечивает AMI-supported SSM agent и завершает cloud-init; он не вызывает document plugins и не является bootstrap launcher. Environment CloudFormation объявляет bootstrap `AWS::SSM::Document` с `DocumentType: Command` и `UpdateMethod: NewVersion`. Поскольку CloudFormation не поддерживает drift detection для этого resource, managed-node orchestrator после stack apply явно требует `Status=Active`, exact numeric `DefaultVersion=LatestVersion`, ожидаемый document-content identity и system-created SHA-256 из `DescribeDocument`/`GetDocument`, затем передаёт те же numeric `DocumentVersion`, `DocumentHash` и `DocumentHashType=Sha256` в `SendCommand`; `$LATEST`, `$DEFAULT` и вызов только по name запрещены. Его `aws:downloadContent` steps получают exact Python 3.14 runtime artifact и content-addressed bootstrap bundle из private content-addressed S3 objects, а следующий минимальный `aws:runShellScript` проверяет recorded SHA-256, извлекает artifacts в private root и вызывает root `host_bootstrap.py`. Package installation, host manifest parsing, EBS initialization, k3s, systemd и network hardening внутри UserData/SSM shell запрещены.

Bootstrap bundle, Python runtime artifact, exact SSM document version и их digests являются immutable inputs compute change set. Python package `host/bootstrap/` разделяет artifacts, storage, network, k3s и services, а `manager.py` владеет только idempotent sequence and dependency wiring. Каждый owner имеет redacted diagnostics и повторяемую acceptance; shell launcher не становится параллельной implementation. UserData и Command document не содержат infrastructure/Product source, Product secrets, deployment policy или GitHub credentials.

До compute change set trusted operator resolver один раз выбирает latest stable release внутри объявленных линий `AWS CLI 2`, `Docker stable/noble`, `uv 0`, `Python 3.14`, `k3s 1.36` и `Helm 4`. Docker signing keyring обязан содержать ровно один primary key с закреплённым fingerprint; его subkeys допустимы, а второй primary trust anchor запрещён. `InRelease` проверяется через `gpgv`, а package digests берутся только из этого подписанного metadata graph. Exact uv tag/commit владеет Python download metadata; source сохраняет selector `3.14`, а canonical host-artifact manifest получает конкретные patch, build, URL и SHA-256. Shared download/verification owners скачивают и проверяют все bytes operator-side; provider modules владеют только своим resolution/install contract, а facade `host/artifact/resolver.py` выполняет dispatch без provider implementation branches.

Canonical bootstrap bundle, Python runtime artifact, host-artifact manifest, SSM document version и их digests являются единственными CloudFormation bootstrap inputs; отдельные дублирующие URL/version/digest parameters запрещены. Instance profile получает только необходимый read boundary exact environment artifact objects и SSM managed-node operation. Create/replacement flow ждёт exact command/plugin results, проверяет downloaded bytes повторно в launcher и доказывает mount, k3s, node и controller readiness до отключения replacement guard. Обычный stop/start переиспользует уже установленный controller и не выполняет source deploy. Exact uv binary определяется digest; semantic version check сравнивает стабильные поля `uv <version>`, а информационный target-triple suffix standalone binary не является частью version identity.

Обычный compute apply сначала создаёт новую immutable LaunchTemplate version, продолжая адресовать работающий instance его текущей exact version. Если latest отличается, orchestrator выполняет существующий controlled replacement: включает guard, останавливает старый instance, переключает active launch-template version и instance slot, повторно подключает retained volume, запускает новый host и выполняет retained Product recovery. Поэтому изменение moving selector не вызывает незащищённый implicit replacement, а повторный replacement из той же stack version использует те же exact artifacts. Retained recovery требует byte-for-byte равенства exact host-artifact manifest retained Product release и active host; compatibility по одной runtime line или selector запрещена. Host artifact update при существующем несовпадающем Product release требует approved pre-production Product reset и нового deploy, а не запуск старого release на новом host contract.

Отсутствие либо другая версия bootstrap artifact не компенсируются package-manager latest, непроверенным install script или состоянием workstation. Unversioned installer URL, `curl | sh`, повторный `uv python install 3.14` и third-party `apt install` без exact package version запрещены.

## AWS Credentials В Runtime

EC2 получает platform role через instance profile. EC2 temporary credentials автоматически обновляются AWS и являются единственным host credential source.

Development environment adapter явно устанавливает `PlatformCredentialDeliveryMode=credential_process`; это единственный слой, который добавляет projected credential volume и credential-specific readiness к common static и dynamic trusted workloads. Common Product base остаётся `ambient` и не содержит EC2 credential path, поэтому тот же runtime используется будущим production Pod Identity без параллельной реализации. Development может материализовать один logical PostgreSQL password в разных owner-local Secrets, но не объединяет их имена: production adapter может выдать каждой workload family отдельную RDS identity без изменения common runtime.

Product-owned host credential refresher устанавливается из exact current WCC release как systemd oneshot/timer, использует `aws configure export-credentials --format process` и атомарно обновляет узко распространяемый Kubernetes `Secret` со стандартным `credential_process` JSON. Backend, platform workers, run-local control services и bounded trusted materialization Jobs монтируют credential directory read-only без `subPath` и используют standard AWS SDK credential chain, которая видит атомарную смену Secret и обновляет session до expiry без Pod restart или ручного apply. Их readiness проверяет timezone-aware expiration и workload health без вывода credential fields. AWS access key, secret и token не передаются через environment variables или command arguments.

Каждый deploy и retained recovery выполняет live rotation gate на уже готовом
backend Pod. Host через действующую platform session получает краткую
tenant-role STS session, передаёт её только stdin-обновлением namespace-local
Secret, а процесс внутри того же immutable Pod UID ждёт exact STS ARN через
standard `credential_process`. В обязательной `finally`-ветке host
восстанавливает instance-profile platform session, снова проверяет её из того
же Pod и только затем продолжает acceptance. Credential fields не попадают в
argument, stdout, manifest или retained export.

Workflow, browser, VPN provider, WorkflowSource candidate test и credentialless wait Pods никогда не получают platform credentials. Trusted materializer передаёт им только exact snapshot и atomic readiness marker через private run/attempt volume. Temporary AWS credentials не входят в retained secret archive, release manifest или logs. Long-lived development Kubernetes Secret archive очищается через 30 дней после замены либо удаления и дополнительно каждые 15 минут credential-refresh timer даже при неизменном current export, кроме exact current set и state, которое Product recovery contract обязан удерживать. Истечение credential без успешного refresh закрывает AWS-dependent operations; static key, anonymous access и local S3 fallback отсутствуют. Истечение laptop SSO session не останавливает уже работающий Product.

## Доставка Исходников И Release

Локальный orchestrator принимает только чистые exact checkouts Product и infrastructure repositories. Primary source graph использует clean/pushed main. Task operation с exact `--git-worktree <common-prefix>` использует same-prefix task worktrees для participating repositories, проверяет branch/root identity и exact upstream commit, а unchanged repositories берёт только из clean/pushed main; соседний worktree или primary checkout никогда не выбирается по proximity. Moving reusable dependencies разрешаются отдельно один раз по `design/environment-model.md`: standard `workflow-container-contract` declaration указывает remote default-branch `HEAD`, resolver получает exact commit и добавляет exported tree в immutable release source graph. Исходники передаются через rsync поверх SSH-over-SSM; EC2 и Docker builds не выполняют Git resolution и не хранят GitHub credentials.

Передаваемый набор содержит только tracked required files и exact exported moving-source trees. Orchestrator создаёт content manifest, проверяет его на host и помещает exact source release на retained volume. Product current links переключаются атомарно только после полной Product acceptance. Release manifest сохраняет environment identity, exact host artifact manifest, repository URLs, requested selectors, resolved symbolic refs и commit SHAs, package versions, archive/content digests, file manifests, runtime platform, immutable image digests, exact Helm chart versions/archive digests, release-local ingress-nginx source URL/digest, digest применённого Kubernetes render и timestamp deployment. Host preparation принимает retained release только если source manifest принадлежит той же environment и его host-artifact payload byte-for-byte равен уже установленному immutable host manifest; latest stack parameters или artifact resolver state не могут задним числом изменить действующий instance/release.

Все Product lifecycle commands всегда передают exact `--environment-name`. Infrastructure operator принимает `--git-worktree <common-prefix>` для task operations, выводит machine name сам и передаёт его Product. Отсутствующий task selector, fallback task checkout на `primary` и прежний CLI contract не поддерживаются. Primary local tunnel port равен `8080`; task port выводится deterministic с collision check, сохраняется environment-local и передаётся automation как exact endpoint.

Platform images собираются на EC2 через Docker Buildx для exact target platform и публикуются в persistent cluster-local OCI registry по immutable digest. Все consumers `workflow-container-contract` получают один exact named source context данного release; `requirements.txt` не разрешает moving Git dependency, а Dockerfile не выполняет двойную установку moving и exact package. User `WorkflowSourceVersion` images собираются в disposable cluster-local rootless BuildKit Pods и принимаются только после platform и publisher validation. Runtime registry и accepted workflow images находятся на retained volume.

Host Product management tool использует отдельный content-addressed relocatable Python environment на retained volume. Его identity выводится из exact pinned tool requirements текущего source release; только новый explicit Product deploy может разрешить и установить новый dependency graph, причём использует exact host Python и не может скачать другой interpreter. Replacement/recovery переиспользует snapshot-owned runtime bytes, а отсутствие нужного environment является fail-closed ошибкой без package index, Git или другого network fallback.

Deployment сначала сохраняет versioned ingress-nginx manifest и exact Helm chart archives внутри immutable release, затем собирает все images и source-map artifacts и фиксирует exact image digests в одном Kustomize render. Install не применяет remote URL или mutable chart reference напрямую. Release становится active только после rollout readiness, Product smoke и exact current-assets verification. При failure восстанавливаются previous Helm revisions, previous release-local ingress manifest и previous exact render; частично собранный набор не становится current.

Replacement или snapshot recovery сначала публикует на disposable root только текущий trusted infrastructure control source. Перед отключением replacement guard он атомарно сохраняет на retained volume environment- и release-bound marker незавершённого Product recovery. Marker считается useful Product activity, блокирует idle shutdown и удаляется только после полной recovery acceptance; поэтому следующий `apply` возобновляет тот же Product recovery даже если прежний orchestrator остановился уже после отключения guard.

Trusted control source требует exact current versions обоих release manifests, их environment/host identities, exact source/render digests и каждый tracked source byte до восстановления `/opt/workflow-infrastructure/current`. Любое выполнение Python непосредственно из infrastructure либо Product source release использует одновременно `-B` и `PYTHONDONTWRITEBYTECODE=1`; current WCC самостоятельно владеет тем же guard для host services и Helm post-renderer. Recovery не изменяет retained source: любой дополнительный, отсутствующий или изменённый byte, включая `__pycache__/*.pyc`, отклоняет release. Отсутствующая или прежняя manifest version, source sanitation и infrastructure-side Product compatibility installer запрещены.

Все host-side Product tool invocations, включая systemd timers, до dependency bootstrap устанавливают environment-exclusive `HOME` в disposable Product cache вне `sources/**`. Infrastructure host controller, не имеющий runtime dependencies вне standard library, исполняется exact host Python напрямую и хранит `HOME`/working state только под environment-exclusive `/var/lib/workflow-infrastructure/**`; он не создаёт virtual environment внутри source release. Kubernetes discovery cache, package-tool cache и другой автоматически создаваемый runtime state никогда не записываются в immutable release source graph.

После восстановления ссылки Product-owned `recover`:

- принимает release identity и target platform только из сохранённого manifest;
- требует уже сохранённые Helm archives и ingress manifest и никогда не скачивает им замену;
- требует все exact image digests в retained registry;
- восстанавливает retained PostgreSQL, secrets, ZITADEL, GlitchTip и source maps;
- применяет сохранённый exact render и переустанавливает renewable credential service;
- не собирает Product images, не создаёт новый release и не переписывает release manifest.

Таким образом новый disposable k3s восстанавливается из snapshot-owned state с той же release identity только на active host с exact тем же host-artifact manifest. Exact host bytes остаются immutable launch provenance и не подменяются в release; новый Product deploy фиксирует новый active host manifest. Обычный deploy после replacement не является recovery и не может подменять этот контракт.

## Платформа Runtime

Deployment получает `(operatingSystem, architecture)` от всех Kubernetes nodes, которым разрешено выполнять `WorkflowRun`. Все eligible nodes обязаны сообщить одну одинаковую platform; смешанный набор отклоняется. OS обязана быть Linux из-за Unix sockets, `fsGroup`, Linux network namespaces и `/dev/net/tun`; process freezing не является runtime requirement.

Node platform нормализуется в OCI form и явно передаётся всем Docker/BuildKit builds. Builder process architecture не является источником target. `BUILDPLATFORM`, `TARGETPLATFORM`, `TARGETOS` и `TARGETARCH` используются как стандартные BuildKit inputs, а accepted OCI manifest/config обязаны совпадать с target.

Target platform является частью immutable build attempt и release identity. Смена architecture не переписывает существующие images. В pre-production она требует чистого Product reset и новых build/version identities; multi-architecture publication остаётся отдельным будущим design.

`browser-runtime` использует bundled Playwright Chromium, доступный для target platform, и не зависит от установленного Google Chrome или Playwright channel `chrome`.

## Автоматическая Остановка

Early stop разрешён после непрерывных 30 минут, в течение которых:

- нет connected Session Manager sessions для instance;
- WCC activity probe доказывает отсутствие незавершённых WorkflowRun, builds, VPN validations, Data/Athena operations, recovery и обязательного cleanup.

CPU, load average и network counters не участвуют в решении. Ошибка Product probe либо невозможность доказать отсутствие Session Manager session считается `busy`, сбрасывает idle-интервал и при доступном Scheduler продолжает renewable lease. Перед stop controller повторно проверяет условия, cordon-ит node, выполняет graceful drain, останавливает Product containers и k3s и вызывает poweroff. Failure снимает cordon и оставляет instance работающим.

Перед каждым explicit `StartInstances` внешний controller создаёт renewable one-time EventBridge Scheduler action на `now + 2 hours`. Target является стабильной stack-owned Lambda, которая при expiry находит running `DevelopmentInstance` exact environment по CloudFormation system tags and environment identity и вызывает `StopInstances`. Lambda не получает Product credentials или данные и имеет только environment-limited `StopInstances`, read-only instance discovery и собственные logs.

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

Максимальный fixed gp3 checkpoint равен `260 GiB`: `100 GiB` root/scratch, `80 GiB` current retained volume и не более одного `80 GiB` предыдущего rollback volume. Семь daily AWS Backup recovery points создаются только для current retained volume. Они физически используют EBS snapshot storage; консервативная верхняя граница billed storage равна `7 × 80 GiB = 560 GiB`: incremental block reuse может уменьшить фактический объём, но не используется как недоказанное условие расчёта максимума.

Перед compute apply cost review получает из AWS Price List API текущие exact
regional price dimensions для S3 Standard storage и requests, customer-managed
KMS keys и requests, Glue Data Catalog storage и requests, Athena scanned data,
S3 validation-nonce requests и Internet data transfer. Для tiered meters сохраняются
все диапазоны, а не один выбранный rate. Их утверждённый usage baseline не
превращается в выдуманное фиксированное потребление: записывается, что quantity
архитектурного delta равна нулю, пока соответствующий contract не изменён.

## Task Environment Cleanup

Root `worktree-bootstrap.yaml` schema v2 объявляет closed project-owned hook `python development_environment_manage.py destroy --git-worktree {common_prefix}` как direct argv без shell. `agent-workflows:goal-brainstorm` валидирует declaration и bind-ит её в sealed task state, но никогда не запускает удаление. При explicit task deletion `agent-workflows:goal-delete` передаёт closed schema-v1 JSON request через stdin с тем же common prefix и journaled cleanup operation identity; `destroy` отвергает invocation без exact matching request и связывает им result. До первого `apply`/`deploy --git-worktree` current schema validation однократно записывает content-free cleanup binding receipt и mutating create/update operations проверяют его exact schema, common prefix, sealed-specification hash, normalized declaration hash, current manifest fingerprint и provider-state generation. Explicit `destroy` не зависит от сохранности этого receipt или прежнего manifest fingerprint: его authority приходит из exact `goal-delete` request, а mutation scope повторно доказывается по non-primary environment identity и AWS ownership. Goal completion сам по себе hook не запускает.

`destroy` является resumable idempotent operation exact task environment. Ни один stack или resource не обязан сохраняться до начала cleanup: уже отсутствующий exact target является успешным шагом. Resolver объединяет доступные stack outputs с deterministic bucket names и всеми remaining EC2 instances, retained volumes и KMS keys, несущими exact `EnvironmentName` + `git-worktree` ownership; поэтому partial prior stack/resource deletion не скрывает оставшиеся goal resources. Он закрывает SSM sessions и stop leases, удаляет либо завершает все task instances и compute stack, а затем data stack до очистки retained buckets, чтобы сначала отозвать CloudFormation-owned IAM writers. После этого он удаляет все versions/delete markers/multipart uploads task buckets, все task retained volumes и one-time copy snapshots. Exact task KMS alias удаляется, все task keys отключаются и принимаются только после proof `PendingDeletion` с minimum AWS waiting period; физическое завершение service deletion не удерживает task cleanup. Финальное доказательство составляют service-native владельцы: CloudFormation проверяет отсутствие stacks, EC2 — accepted terminated/absent instances, volumes и snapshots, S3 — отсутствие buckets, KMS — отсутствие alias и accepted `PendingDeletion`/physical absence. Eventually consistent Resource Groups Tagging API используется для discovery, но его устаревшие tombstones не являются blocking absence proof. Account-foundation, primary, resources другого task prefix и untagged account-global guards не затрагиваются.

Successful hook возвращает closed machine-readable result с exact request schema, common prefix, cleanup operation identity и `external_resources_absent: true`; immutable inventory и phase progress остаются в private durable journal до завершения общего cleanup. `git-worktree` tag inventory является discovery source, а не blanket deletion selector или финальный consistency oracle. Foreign ownership и KMS state outside accepted transition закрывают truthful success; stale записи tag index после service-native deletion не закрывают. Git worktrees, branches и private task state удаляет `agent-workflows:goal-delete` только после этого result; permanent goal registry directory сохраняется.

## Разрушающий Cutover

До первого current-format deploy infrastructure-owned destructive reset является обязательной стадией одного exact candidate deploy и оркестрирует два явных владельца. Old project-prefixed stacks/resources, public VPN validation API Gateway/Lambda и embedded-Bash bootstrap contour уничтожаются вместо alias/compatibility support; `account-foundation`, `data-<environment>` и `compute-<environment>` создаются как единственные current owners. Candidate Product source поднимает retained PostgreSQL, ZITADEL и GlitchTip без старого registry и Product workloads; Product-owned reset доказывает сохранённые identities до/после операции и удаляет БД `apwid` и `apwid_test`, workflow registry, WorkflowRun storage, все development Data/Secret/Result object versions и dynamic Glue/Athena catalog. После успешной Product-проверки host-local retained-release owner удаляет прежние retained Product releases, pointers, recovery marker и Product-tool runtime contents, сохраняя candidate source. Тот же deploy немедленно создаёт чистый Product-tool runtime, собирает и активирует candidate. Операция не интерпретирует прежний manifest и не имеет отдельного состояния между reset и deploy.

Конкретные Product database migration cases и workflow input migration edges удалённого pre-cutover state отсутствуют. Reusable database migration verifier и workflow input migration graph/loader остаются частью current Product source. Schema/data compatibility bridge, dual Product deployment, old/new data-state synchronization, завершённый one-time logical dump/import contour, Local Kubernetes branch и перенос прежнего registry отсутствуют.

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
- Product source, secrets или deployment policy в UserData либо bootstrap Command document.
- Package install, EBS/k3s/systemd/network implementation in UserData/SSM shell вместо verified Python bootstrap owners.
- Public VPN validation endpoint, validation Pod AWS credentials или exit-IP report.
- Project-prefixed AWS account-local names, project tag, task fallback to `primary` или deletion только по tag.
- Builder-default platform, hardcoded `linux/arm64` или silent image rewrite при смене architecture.
- CPU/load/network-based idle decision или абсолютный max uptime здорового instance.
- Raw copy несовместимого local registry либо потеря ZITADEL/GlitchTip при Product reset.

## Проверки

CloudFormation acceptance доказывает exact account/region, единственного `account-foundation` owner, environment stack ownership, отсутствие project-prefixed AWS identities, exact task tag `git-worktree`, retained policies, IAM trust, private nonce validation без API Gateway, VPC routes, отсутствие ingress, IMDSv2, Scheduler lease, encrypted Session Manager shell logging, SSM-agent-only UserData, active exact numeric bootstrap Command document version/system SHA-256 и их binding в SendCommand, artifact checksum failure path и primary-only AWS Backup plan с семью recovery points. Cost review использует текущие цены и явно записанные usage assumptions.

Deployment acceptance доказывает clean exact source manifests, одно разрешение moving `workflow-container-contract` на release, одинаковую exact contract identity во всех platform consumers, отсутствие Git resolution внутри builds/recovery, native target platform, immutable image digests, registry persistence и live allowed/denied registry paths, credential rotation в том же Pod с обязательным восстановлением platform session, rollback previous release, Product readiness, source-map publication и отсутствие credentials в source, logs, images и retained archives.

Operational acceptance доказывает logged ordinary SSM shell, metadata-only port forwarding/SSH-over-SSM, 30-minute idle stop, useful-work lease renewal beyond two hours, fail-safe stop after renewal loss и все три primary recovery scenarios. Task-environment acceptance доказывает derived identity, full-prefix collision guard, independent stacks/state/tunnel, отсутствие scheduled backup и no fallback to primary. Product acceptance следует `workflow-control-center` и выполняется через exact environment assets по SSM tunnel.

Provider-independent lifecycle policy проверяется controlled clock. Отдельная
operator-only `lifecycle-acceptance` использует фиксированную сокращённую
конфигурацию только для реального AWS перехода create-renew-expire-delete,
останавливает обычный development lifecycle controller на время проверки и всегда восстанавливает
обычные интервалы, controller и Product readiness. Сокращённые интервалы не являются
steady-state configuration development service.
