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
python tool/development_environment_manage.py <command>
```

Команды не принимают или не печатают static credentials. До AWS mutation entrypoint проверяет identity, region, stack state и renewable stop lease.

## Создание И Обновление Инфраструктуры

```bash
python tool/development_environment_manage.py apply
```

`apply` проверяет оба шаблона, создаёт и инспектирует change sets data-plane и compute stacks, применяет разрешённые изменения и проверяет outputs, retained-resource identities, Session Manager readiness и host bootstrap. Existing stable data-plane stack обновляется in place; compute stack управляется отдельно.

## Запуск И Остановка

```bash
python tool/development_environment_manage.py start
python tool/development_environment_manage.py stop
python tool/development_environment_manage.py status
```

`start` сначала создаёт one-time external stop schedule на два часа и не запускает instance, если schedule не создан. После start команда ждёт EC2, SSM, retained mount, k3s и host controller readiness.

`stop` выполняет тот же graceful cordon/drain/service-stop path, что и idle controller, затем удаляет pending schedule после доказанной остановки. `status` показывает instance, SSM sessions, stop lease, retained volume, latest snapshot, k3s, current release и безопасный WCC activity summary без secret values.

## Подключение

```bash
python tool/development_environment_manage.py connect
python tool/development_environment_manage.py ssh -- <ssh-arguments>
```

`connect` держит одну SSM port-forwarding session от remote ingress к `localhost:8080`. Product UI открывается по `http://localhost:8080`; ZITADEL и GlitchTip используют соответствующие `*.localhost:8080` hostnames. Команда является long-running foreground process, чтобы состояние tunnel оставалось видимым.

`ssh` использует SSH-over-SSM и поддерживает обычные SSH, SCP, rsync и Remote SSH IDE flows без inbound port `22`. Persistent SSH private key или public ingress не создаются.

## Развёртывание Product

```bash
python tool/development_environment_manage.py deploy
```

`deploy`:

1. определяет все необходимые source repositories из Product release contract;
2. проверяет clean worktrees, exact upstream commits и remote URLs;
3. передаёт только tracked required files через rsync over SSH-over-SSM;
4. проверяет content manifest на host и атомарно публикует source release;
5. определяет единую Linux OCI platform по eligible Kubernetes nodes;
6. сохраняет exact Helm charts и release-local ingress-nginx manifest с SHA-256 provenance;
7. собирает platform images и source maps, публикует immutable digests в retained local registry;
8. вызывает Product-owned secret restore, renewable credential refresh, render/apply/smoke path из переданного WCC source;
9. активирует release и переустанавливает host credential timer только после полной readiness/smoke либо восстанавливает previous Helm revisions, ingress и exact render.

Команда не выполняет `git clone` на host и не сохраняет GitHub credentials. Dirty или unpublished source блокирует deployment. AWS credential-process JSON не входит в source/release manifest, retained secret export или operator output; его обновляет Product-owned systemd timer из exact current WCC release.

После deploy автоматические и ручные UI проверки выполняются через уже открытый SSM tunnel против exact current assets.

## Диагностика

```bash
python tool/development_environment_manage.py diagnose
```

`diagnose` собирает безопасный снимок CloudFormation, EC2, SSM, stop lease, volume/snapshot, disk pressure, k3s node, namespaces, workloads, events, registry, current/previous release и Product-owned diagnostics. Он не выводит credential process JSON, Kubernetes Secret values, database content, VPN configuration или Product input.

AWS, infrastructure, cluster и Product findings показываются раздельно. Неуспешный Product smoke не маскируется готовностью instance или k3s.

## Восстановление

```bash
python tool/development_environment_manage.py restore --snapshot-id <snapshot-id>
```

`restore` создаёт новый encrypted retained volume из exact snapshot, не изменяя source snapshot и старый retained volume, подключает его к replacement instance и запускает полный recovery acceptance. Переключение current retained volume происходит только после успешной проверки Product и обязательного состояния.

Обычная проверка recovery выполняет:

- stop/start того же instance;
- replacement instance с тем же volume через `python tool/development_environment_manage.py replace`;
- replacement instance с новым volume из snapshot.

Каждый сценарий проверяет ZITADEL user/grants, GlitchTip, Product databases по их заявленному lifecycle, registry, WorkflowRun recovery, AWS data plane, UI и current release identity.

## Разрушающий Product Cutover

Initial cutover с laptop kind сохраняет identity и observability state логическими exports, но пересоздаёт Product databases, workflow state, registry и development Data/Secret/Result/Athena state. Local cluster и его данные остаются на laptop до полной remote acceptance.

Удаление старого local-kind state, старого retained volume, snapshots, identity state или GlitchTip state не является частью обычного deploy/restore и выполняется только отдельной точной операцией.
