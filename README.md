# Vacancy Collector Skill v1.0

Vacancy Collector ведёт локальный реестр вакансий. `IMPORT` принимает JSON и CSV, проверяет записи, выявляет дубликаты и конфликты и добавляет новые вакансии. `MANAGE` меняет статус существующих записей по явным командам.

## Требования

Windows, PowerShell и Python 3.13.5, доступный командой `python`. Используется стандартная библиотека Python. Блокировка реестра использует модуль Windows `msvcrt`.

| Путь | Содержимое |
| --- | --- |
| `.agents/skills/vacancy-collector/` | Инструкция навыка и CLI в `scripts/import_vacancies.py` |
| `TECHNICAL_SPECIFICATION.md` | Схема данных, правила импорта, статусов и записи |
| `tests/` | Автоматические тесты и вымышленные фикстуры |
| `input/`, `data/`, `outputs/` | Локальные входы, реестр и результаты запусков |

## Первый запуск IMPORT

Выполните команды PowerShell из корня чистой рабочей копии комплекта. Пример рассчитан на пустой реестр. Если в `input/` уже есть файлы, используйте отдельную копию, чтобы сохранить их.

```powershell
New-Item -ItemType Directory -Force -Path input, data | Out-Null
@(
    'Source;External_ID;Vacancy_Title'
    'example;001;Example analyst'
) | Set-Content -LiteralPath input/vacancies.csv -Encoding utf8
python -B .agents/skills/vacancy-collector/scripts/import_vacancies.py IMPORT input/vacancies.csv
```

Команда выведет идентификатор запуска и `OK`. В `data/vacancies_registry.csv` появится запись `VC-000001` со статусом `ACTIVE`. Для собственного файла укажите путь внутри `input/`. Обязательные поля — `Source`, `External_ID` и `Vacancy_Title`. Для CSV используйте UTF-8 с BOM или без него и разделитель `;`. JSON принимает массив вакансий или объект с массивом `items`.

## Запуск MANAGE

После примера IMPORT выполните команды в той же копии. Для существующего реестра предварительно согласуйте изменение статуса и возьмите `Internal_ID` нужной записи из реестра.

```powershell
@(
    'Internal_ID;Action;Reason'
    'VC-000001;EXCLUDE;Example exclusion'
) | Set-Content -LiteralPath input/record_actions.csv -Encoding utf8
python -B .agents/skills/vacancy-collector/scripts/import_vacancies.py MANAGE
```

После `OK` статус примера будет `EXCLUDED`. `ARCHIVE` ставит `ARCHIVED`, а `RESTORE` возвращает `ACTIVE`. Для `EXCLUDE` и `ARCHIVE` обязательна непустая причина. Для `RESTORE` причина может быть пустой. Повтор того же импорта сохраняет существующую запись и её статус.

Журнал каждого запуска находится в `outputs/logs/`. Отчёт и CSV с отклонениями находятся в `outputs/reports/`. Их имена содержат идентификатор запуска. Держите реестр, файлы команд и результаты локально. В отчётах на уточнение могут сохраняться исходные значения.

## Тестирование

Выполните из корня рабочей копии. Тесты создают отдельные каталоги `tests/scratch-*` и удаляют их при завершении.

```powershell
python -B -m unittest discover -s tests -v
```

## Ограничения

- Версия работает с локальными данными. Внешние API, автоматические отклики, оценка вакансий и планировщик не входят в её возможности.
- При просмотре в Excel импортируйте `External_ID` как текст, чтобы сохранить ведущие нули. Классический мастер Excel может разбивать поля со встроенными переносами строк. В CDsk01 проверка AT-06 была принята для отдельной копии CSV, подготовленной для Excel.
- Независимая проверка обнаружения и применения навыка в новом сеансе Codex (AT-07) отложена. Прямой запуск CLI выполняется командами выше.
