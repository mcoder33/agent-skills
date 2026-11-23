# Установка в Claude Code

[К обзору](../README.md) · [Установка в Codex](installation.md) · [Примеры использования](usage.md)

Репозиторий одновременно является маркетплейсом плагинов Claude Code: файл [.claude-plugin/marketplace.json](../.claude-plugin/marketplace.json) раздаёт навыки прямо из каталога `skills/`, без копирования. Нужен Claude Code 2.1.275 или новее.

## Что устанавливается

| Плагин | Навыки | Вызов |
| --- | --- | --- |
| `codex-skills` | clear-communication, adaptive-dialogue, dialogue-explore, dialogue-validate, nasa, effort, checks | `/codex-skills:<навык>` |
| `go-development` | go-development, ub-mode | `/go-development:go-development`, `/go-development:ub-mode` |
| `grafana-dashboards` | grafana-dashboards | `/grafana-dashboards:grafana-dashboards` |

Плагины независимы: ставьте только нужные. Если такой же навык уже приходит из другого плагина, второй экземпляр не ставьте — иначе при автоматическом выборе Claude увидит два навыка с одинаковым описанием.

Не входят в плагины навыки, которые опираются на механику Codex: `model-router` и `orchestrate` (модели `gpt-*`, TOML-профили подагентов), `delegation-stats` и `session-retrospective` (журналы сессий Codex). В Claude Code для делегирования используются собственные подагенты.

## Установка

В сеансе Claude Code — одной командой (маркетплейс добавляется автоматически):

```text
/plugin install codex-skills --marketplace mcoder33/agent-skills
```

Или в два шага:

```text
/plugin marketplace add mcoder33/agent-skills
/plugin install codex-skills@codex-skills
```

Из shell:

```sh
claude plugin marketplace add mcoder33/agent-skills
claude plugin install codex-skills@codex-skills
claude plugin install go-development@codex-skills       # по необходимости
claude plugin install grafana-dashboards@codex-skills   # по необходимости
```

Проверьте состав: `claude plugin details codex-skills` должен показать `Skills (7)`. Затем откройте новый сеанс и отправьте, например:

```text
/codex-skills:clear-communication Объясни этот план понятным языком:
начни с результата, затем приведи основания и важные ограничения.
```

Навыки также подключаются автоматически по описанию, без явного вызова.

Для закреплённой версии добавьте маркетплейс с тегом: `claude plugin marketplace add mcoder33/agent-skills@<тег>`.

### Из локального клона

Для разработки навыков добавьте клон как маркетплейс из каталога — правки в `skills/` подхватываются в следующем сеансе или по `/reload-plugins`, без переустановки:

```sh
claude plugin marketplace add ./agent-skills
claude plugin install codex-skills@codex-skills
```

## Синтаксис вызова

В примерах навыков используется синтаксис Codex `$навык`. В Claude Code ему соответствует `/плагин:навык` с аргументами в той же строке: `$effort low` → `/codex-skills:effort low`. Два навыка в одном запросе вызываются по очереди или словами: «effort low, checks high».

## Постоянный режим и карта Obsidian

Аналог фрагментов `AGENTS.md` для Claude Code — [CLAUDE.fragment.md](../config/CLAUDE.fragment.md). Вставьте его в `~/.claude/CLAUDE.md` (для всех проектов) или в `CLAUDE.md` проекта и замените `{{DIALOGUE_VAULT}}` абсолютным путём к vault, например `$HOME/Documents/Obsidian/Codex-Dialogue`. Без vault удалите строку `DIALOGUE_VAULT` — dialogue-навыки будут вести карту в чате.

Перед вставкой сохраните копию файла; при обновлении заменяйте блок `BEGIN/END`, не добавляя дубль. Подготовка самого vault описана в [установке для Codex](installation.md#карта-обсуждений-в-obsidian) и одинакова для обоих клиентов.

Runtime-helper и hooks из [skill-runtime](skill-runtime.md) написаны для Codex и в Claude Code не подключаются; навыки работают без них по основному контракту карты.

## Обновление и удаление

```sh
claude plugin marketplace update codex-skills
claude plugin update codex-skills@codex-skills
```

Удаление: `claude plugin uninstall codex-skills@codex-skills`; маркетплейс целиком — `claude plugin marketplace remove codex-skills`. Карты и заметки vault не затрагиваются.
