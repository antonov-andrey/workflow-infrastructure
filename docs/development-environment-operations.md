# Работа Со Средой Разработки

## Назначение

Этот документ является операционным владельцем управления реализованной средой разработки `Workflow Control Center`. Архитектурные причины и границы принадлежат `design/development-environment.md`; Product-specific Kubernetes diagnostics и acceptance принадлежат проекту `workflow-control-center`.

## Предварительные Условия

На workstation требуются AWS CLI v2, Session Manager plugin, rsync, SSH client и Python 3.14. Воспроизводимое project environment создаётся прямой командой:

```bash
python tool/venv_create.py
```

Host controller вызывает тот же utility с `--runtime-only` и работает через release-local virtual environment.

Профиль `workflow-control-center-devel` обязан возвращать account `463564115167` и region `us-east-1`.

Единственный entrypoint оператора:

```bash
python development_environment_manage.py <command>
```

Каждая environment operation получает ровно один exact selector: `--environment-name primary` для основной среды либо `--git-worktree <task-common-prefix>` для task environment. Task selector сам выводит machine name `w<15-sha256-hex>`, проверяет full-prefix collision и никогда не fallback-ится на primary. До первой task AWS mutation он дополнительно требует sealed private-state binding exact schema-v2 cleanup declaration; отсутствие binding закрывает `apply`/`deploy`. Команды не принимают или не печатают static credentials. До AWS mutation entrypoint проверяет identity, region, account-foundation, stack state и renewable stop lease.

## Создание И Обновление Инфраструктуры

```bash
python development_environment_manage.py account-foundation-apply
python development_environment_manage.py apply --environment-name primary
python development_environment_manage.py apply --git-worktree <common-prefix>
```

`account-foundation-apply` является отдельным primary-only bootstrap/reconciliation шагом: он проверяет exact AWS account/region и clean+pushed `workflow-infrastructure` source, затем создаёт либо обновляет единственного account-global owner без неявного создания EC2 или environment data plane. `--git-worktree` для этой команды запрещён. `apply` для primary также проверяет/reconciles `account-foundation`, затем exact `data-primary` и `compute-primary` templates/change sets и после получения retained-volume ARN повторно reconciles foundation-owned single AWS Backup selection. Task apply не создаёт и не изменяет foundation: он требует уже принятого live state, затем применяет exact `data-<task-environment>` и `compute-<task-environment>`. Команда проверяет outputs, retained-resource identities, Session Manager readiness, SSM-agent-only UserData, active exact numeric bootstrap Command document version/system SHA-256, передачу тех же version/hash в SendCommand, download checksums и Python host bootstrap result. Новый base retained volume имеет `RetainedVolumeFilesystemState=pending`; после exact AWS ownership/attachment/no-snapshot proof bootstrap сериализованно создаёт и монтирует XFS, затем защищённый compute change set переводит его в `complete`. Проверка raw bytes на нули запрещена, поскольку логически пустой EBS может содержать криптографически псевдослучайные блоки. Restore volume сразу обязан быть `complete`, а уже принятый volume без XFS останавливает операцию без форматирования. Environment stacks не владеют account-global guards. Ordinary compute change set не имеет права неявно заменить instance, retained volume или attachment. Если exact launch input изменился, `apply` включает external guard, доказывает остановку и detach, меняет instance slot, подключает тот же retained state и завершает recovery acceptance только для exact current-format release с тем же host-artifact manifest. Отдельные `replace` и `restore` остаются явными operator recovery commands, а не обязательным продолжением обычного apply.

Если процесс прерван после успешного создания replacement instance, повторный `apply` распознаёт оставшийся `ReplacementGuardScheduleState=ENABLED`, сначала устанавливает exact control source на уже созданный host и завершает retained Product recovery, затем отключает guard и продолжает с последнего безопасного состояния. Завершённый `UPDATE_ROLLBACK_COMPLETE` является допустимой stable recovery точкой для нового change set; незавершённый rollback и произвольный drift не принимаются.

Если cloud-init созданного host завершился terminal error до mount retained root и до запуска k3s, `apply` не пытается объявить такой host восстановленным: сохраняет guard, применяет исправленную более новую launch-template version и заменяет disposable host тем же controlled replacement workflow. Mounted retained root, active k3s, неоднозначный cloud-init status или отсутствие новой version требуют остановки и диагностики вместо автоматической замены.

Data-plane template может превышать inline limit CloudFormation. В этом случае `apply` автоматически публикует exact content-addressed template в retained Observability bucket, проверяет S3 checksum/metadata и использует private `TemplateURL`; вручную загружать template либо делать bucket публичным не требуется. Artifact lifecycle равен 30 дням.

## Запуск И Остановка

```bash
python development_environment_manage.py start --environment-name primary
python development_environment_manage.py stop --environment-name primary
python development_environment_manage.py status --environment-name primary
```

`start` сначала создаёт one-time external stop schedule на два часа и не запускает instance, если schedule не создан. Schedule вызывает stack-owned tag-resolving stop function. CloudFormation create/replacement отдельно защищает stack-owned replacement guard, который включается до instance по dependency и выключается только после readiness и доказанного renewable lease. После start команда ждёт EC2, SSM, успешный cloud-init, exact retained mount, k3s, Kubernetes node и host controller readiness. Внутренние create/replacement flows после cloud-init устанавливают exact проверенный infrastructure source и controller до финального proof; обычный lifecycle-only `start` переиспользует уже установленный controller и не превращается в неявный deploy.

`stop` выполняет тот же graceful cordon/drain/service-stop path, что и idle controller, затем удаляет pending schedule после доказанной остановки. `status` показывает instance, SSM sessions, stop lease, retained volume, latest snapshot, exact primary-only AWS Backup policy state, k3s, current release и безопасный WCC activity summary без secret values. Для `primary` одного `CloudFormation` `CREATE_COMPLETE`/`UPDATE_COMPLETE` недостаточно: фактические plan rule, vault, exact current-volume ARN selection и current-volume tag должны совпасть. Для дополнительной environment status доказывает `NOT_APPLICABLE` и отсутствие regular-backup tag.

Реальная сокращённая проверка AWS lifecycle выполняется отдельно и является
разрушающей:

```bash
python development_environment_manage.py lifecycle-acceptance --environment-name primary
```

Она допускается только при `WCC activity=idle`, не меняет steady-state development policy
`30 minutes / 2 hours`, временно останавливает обычный host controller и на том же
EventBridge Scheduler target доказывает create, renewal после исходного deadline,
fail-safe stop после прекращения renewal и `ActionAfterCompletion=DELETE`. Затем
команда запускает тот же instance, восстанавливает обычный двухчасовой lease,
controller, k3s и Product recovery acceptance. Любая промежуточная ошибка запускает
тот же restoration path.

## Подключение

```bash
python development_environment_manage.py connect --environment-name primary
python development_environment_manage.py ssh --environment-name primary -- <ssh-arguments>
```

`connect` держит одну SSM port-forwarding session от remote ingress. Primary использует persisted local port `8080`; task environment получает deterministic collision-checked persisted port, который `connect` и `status` печатают как exact endpoint. ZITADEL и GlitchTip используют соответствующие `*.localhost:<environment-port>` hostnames. Команда является long-running foreground process, чтобы состояние tunnel оставалось видимым.

`ssh` использует SSH-over-SSM и поддерживает обычные SSH, SCP, rsync и Remote SSH IDE flows без inbound port `22`. Persistent SSH private key или public ingress не создаются.

Idle controller проверяет только наличие Session Manager session для exact instance. Содержимое port-forwarding и SSH-over-SSM не записывается и для idle-решения не требуется; Session Manager не поддерживает content logging этих tunnel sessions. Ordinary Session Manager shell по account-foundation preferences пишет command/output в encrypted CloudWatch log group с retention, но не становится вторым источником activity truth.

Host bootstrap, Product recovery и recovery acceptance выполняются через SSM Run
Command. Orchestrator опрашивает фактический invocation status до одного часа,
переживает краткую задержку регистрации invocation и при timeout сообщает command
ID, не отменяя удалённую операцию. Её состояние и output после такого timeout
проверяются через `aws ssm get-command-invocation`.

## Развёртывание Product

```bash
python development_environment_manage.py deploy --environment-name primary
python development_environment_manage.py deploy --git-worktree <common-prefix>
```

`deploy`:

1. создаёт либо обновляет stop lease, запускает остановленный idle environment через обычный lifecycle contract и публикует текущий clean infrastructure control source до полной host readiness;
2. определяет все необходимые source repositories из Product release contract;
3. для task selector проверяет exact same-prefix participating worktrees, для unchanged sources — clean/pushed main, а также exact upstream commits и remote URLs;
4. передаёт только tracked required files через rsync over SSH-over-SSM;
5. проверяет content manifest на host и публикует exact source release в retained `release/releases/<release>/`;
6. из exact infrastructure source атомарно устанавливает checksum-pinned Helm для фактической host architecture;
7. определяет единую Linux OCI platform по eligible Kubernetes nodes;
8. сохраняет exact Helm charts и release-local ingress-nginx manifest с SHA-256 provenance;
9. собирает platform images и source maps, публикует immutable digests в retained local registry;
10. вызывает Product-owned secret restore, renewable credential refresh, render/apply/smoke path из переданного WCC source;
11. выполняет live registry gate: exact kubelet pull и read-only proxy reads проходят, private/mutation proxy paths и writer access из builder/постороннего namespace отклоняются, а trusted build-worker write path проходит;
12. временно ротирует backend credential-process на действующую tenant-role STS session, подтверждает её и восстановленную platform session из того же Pod UID без вывода credential fields;
13. после полной readiness/smoke атомарно переключает retained `release/current` и восстановимую root-volume ссылку `/opt/workflow-infrastructure/current`, затем переустанавливает host credential timer; при failure восстанавливает previous Helm revisions, ingress и exact render, не меняя current links.

Команда не выполняет `git clone` на host и не сохраняет GitHub credentials. Dirty или unpublished source блокирует deployment. AWS credential-process JSON не входит в source/release manifest, retained secret export или operator output; его обновляет Product-owned systemd timer из exact current WCC release.

Pre-hardening compute/source/runtime contracts не поддерживаются. До первого `current-format` deploy оператор выполняет утверждённый destructive Product cutover:

```bash
python development_environment_manage.py deploy \
  --environment-name primary \
  --reset-product-state \
  --user-email antonov.andrey@gmail.com
```

Команда публикует exact candidate source, через его Product adapter поднимает только retained PostgreSQL, ZITADEL и GlitchTip, проверяет сохранённые identity/observability boundaries и удаляет disposable Product state. Затем candidate infrastructure source удаляет прежний retained Product release/runtime graph, сохраняя candidate, и тот же deploy немедленно продолжает clean build и activation. Отдельного промежуточного состояния между reset и deploy нет. Операция не читает и не преобразует прежний manifest, не создаёт compatibility symlink и не выполняет migration прежнего Product state.

После deploy автоматические и ручные UI проверки выполняются через уже открытый SSM tunnel против exact current assets.

## Диагностика

```bash
python development_environment_manage.py diagnose --environment-name primary
```

`diagnose` собирает безопасный снимок CloudFormation, EC2, SSM, stop lease, volume/snapshot, disk pressure, k3s node, namespaces, workloads, events, registry, current/previous release и Product-owned diagnostics. Он не выводит credential process JSON, Kubernetes Secret values, database content, VPN configuration или Product input.

Host controller пишет transition-aware предупреждения по root и retained volume в systemd journal: warning при `75%` used или менее `10 GiB` free, critical при `90%` used или менее `5 GiB` free, повтор не чаще шести часов. В доказанном idle состоянии он с тем же шестичасовым cadence вызывает `maintenance` exact current WCC release. Неуспешная maintenance сохраняет данные и отражается как warning; она не подменяет activity probe и не разрешает остановку при неопределённом Product state.

AWS, infrastructure, cluster и Product findings показываются раздельно. Неуспешный Product smoke не маскируется готовностью instance или k3s.

## Удаление Task Environment

Завершение goal не удаляет environment. Когда пользователь явно просит удалить exact task common prefix и он проходит synchronized-cleanup preconditions, `agent-workflows:goal-delete` вызывает recorded project hook:

```bash
python development_environment_manage.py destroy --git-worktree <common-prefix>
```

Не запускайте эту команду вместо `goal-delete` вручную: она отвергает invocation без exact closed JSON stdin request, а resources, worktrees, refs и central task directory должны удаляться одной resumable transaction. Hook закрывает sessions/leases, очищает exact versioned buckets и multipart uploads, удаляет data/compute stacks и retained task volume, удаляет one-time snapshots, затем удаляет KMS alias, disables key и принимает `PendingDeletion` с minimum AWS waiting period. Он возвращает exact machine-readable absence proof, связанный с request operation identity; `git-worktree` tag inventory является leak check, а не blanket deletion selector. Account-foundation, primary и другие task environments сохраняются.

## Восстановление

```bash
python development_environment_manage.py restore --environment-name primary --snapshot-id <snapshot-id>
```

`restore` создаёт новый encrypted retained volume из exact snapshot, не изменяя
source snapshot и старый retained volume, подключает его к replacement instance и
запускает полный recovery acceptance. Snapshot до остановки instance должен быть
`completed`, принадлежать development account, быть encrypted и помещаться в
текущий approved volume size. Snapshot обязан содержать retained
`release/current` и соответствующий exact release; отсутствие или изменение
tracked source, manifests, render, Helm archives, ingress artifact либо registry
digest закрывает recovery.

CloudFormation не обновляет `AWS::EC2::Volume.SnapshotId` in place. Stack декларативно переключает base/`a`/`b` retained-volume resources, поэтому каждый restore создаёт новый physical volume даже при повторном восстановлении. После точной проверки нового volume старый `Retain` volume теряет только primary AWS Backup selection tag: его данные сохраняются, но новые daily recovery points больше не создаются для неактивного volume. Одновременно допускаются current и только один предыдущий rollback volume. Перед следующим restore более старый rollback удаляется лишь после проверки exact stack tags, encryption, размера, KMS key, состояния `available`, пустых attachments и отсутствия regular-backup tag; current volume cleanup не затрагивает. `status` показывает current retained-volume slot и source snapshot.

Дополнительные development environments регулярных snapshots не имеют. Если для копирования их retained volume создаётся одноразовый EBS snapshot, после доказанного создания копии его удаляют вручную; такой snapshot не является backup history.

`replace` и `restore` задают следующий alternating slot и включённый двухчасовой replacement guard, gracefully останавливают старый instance, доказывают detachment retained EBS и только затем исполняют replacement change set. CloudFormation создаёт новую launch-template version и запускает instance лишь после обновления guard; после запуска проверяется exact version из EC2 metadata. При rollback старый volume повторно подключается к stack-declared остановленному instance до возврата ошибки. Обычный `apply` не требует отдельной operator-команды: при обнаружении новой launch-template version он вызывает тот же controlled replacement primitive автоматически.

После запуска replacement instance orchestration выполняет один порядок:

1. публикует и устанавливает текущий trusted infrastructure control source на disposable root;
2. этим source проверяет retained current release manifests и каждый tracked source byte;
3. только после проверки восстанавливает `/opt/workflow-infrastructure/current`;
4. запускает Product-owned `recover`, который использует сохранённые image digests, charts, ingress и render без image build, artifact download, нового release или переписывания manifest;
5. переустанавливает Product credential-refresh service;
6. повторяет live registry gate для восстановленного exact image graph;
7. повторяет same-Pod credential rotation и обязательное восстановление platform session;
8. запускает отдельный полный recovery acceptance.

Обычная проверка recovery выполняет:

- stop/start того же instance;
- replacement instance с тем же volume через `python development_environment_manage.py replace --environment-name primary`;
- replacement instance с новым volume из snapshot.

Каждый сценарий проверяет ZITADEL user/grants, GlitchTip, Product databases по их заявленному lifecycle, registry, WorkflowRun recovery, AWS data plane, UI и current release identity.

## Разрушающий Product Cutover

До первого current-format deploy Product-owned reset сохраняет существующие retained ZITADEL и GlitchTip databases/files на месте, но пересоздаёт Product databases, workflow state, registry и development Data/Secret/Result/Athena state. Логический dump/import из прежней среды и параллельный local-cluster contour отсутствуют.

Удаление current retained volume, единственного текущего rollback volume, snapshots, identity state или GlitchTip state не является частью обычного deploy и выполняется только отдельной точной операцией. Единственное исключение — bounded cleanup более старого rollback volume в начале следующего explicit `restore`.
