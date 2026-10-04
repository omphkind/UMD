# UMD — Universal Media Downloader

Универсальный загрузчик медиаконтента. Первая бета `0.1.0` — Windows x64
приложение с консольным меню для обработки метаданных YouTube.

## Запуск Windows-сборки

1. Скачайте `UMD-v0.1.0-beta.N-windows-x64.zip` со страницы
   [Releases](https://github.com/omphkind/UMD/releases).
2. Распакуйте **весь** архив и откройте `UMD/UMD.exe`.
3. Выберите «Новая обработка», добавьте URL видео, канала, плейлиста или
   несколько URL. Настройки задачи берутся из сохранённых предпочтений.

Python, yt-dlp, Deno и Chromium входят в ZIP. Системная установка не требуется.
Сборка предназначена для Windows 10/11 x64. Настройки, прогресс, архив предыдущих
задач и логи сохраняются в `%LOCALAPPDATA%\UMD`; экспорт — в подкаталоге `output`.
Путь yt-dlp и каталог экспорта можно изменить в меню «Настройки».

Меню: новая обработка, продолжить предыдущую, обновить источник,
повторить только ошибки, настройки, выход. `Ctrl+C` прерывает обработку
с сохранением прогресса. После закрытия окна или сбоя используйте resume.
Update добавляет только новые ID; retry сохраняет исходные номера объектов.

## Возможности первой беты

- YouTube: отдельные видео, каналы, плейлисты, списки URL.
- Оригинальные метаданные через локальный yt-dlp; отображаемые на YouTube
  локализованные название и описание через анонимный Chromium с `ru-RU`.
- Консервативная очистка описания; исходный текст сохраняется в JSON.
- Shorts и Members-only по умолчанию пропускаются с причиной, без ошибки.
- Постоянные ID и номера; прогресс после каждого элемента, резервная копия,
  восстановление прерванных задач и защита от параллельной записи.
- `UMD_URL.txt` содержит только URL; `UMD_META.txt` совместим с MetaFin;
  `UMD_META.json` сохраняет метаданные, их происхождение и историю задачи.

Это metadata-first MVP из задания. Загрузка видео/аудио/субтитров,
графический интерфейс, переводчики, другие источники и Jellyfin — следующие этапы.
Авторизация и пользовательские браузерные cookies не используются; включение
Members-only не предоставляет доступ к закрытым материалам.

YouTube может менять DOM или ограничивать доступ. При сбое локализации
сохраняются оригинальные метаданные и сообщение в логе. Локализованный текст
может совпадать с оригиналом, если автор не предоставил перевод.
Порядок датированных объектов точный; для плоских списков канала без дат
используется порядок источника, поэтому сортировка между вкладками приблизительная.

## Команды

```powershell
.\UMD.exe --check-environment
.\UMD.exe --self-test
.\UMD.exe process "https://www.youtube.com/watch?v=jNQXAC9IVRw"
.\UMD.exe resume
.\UMD.exe update
.\UMD.exe retry
.\UMD.exe history
.\UMD.exe export
.\UMD.exe settings --set debug=true
.\UMD.exe settings --set localization=false
.\UMD.exe --data-dir "D:\UMD\separate-task" process "https://www.youtube.com/@YouTube"
```

Настройки `include_shorts`, `include_members_only`, `localization`, `debug`
принимают `true/false`; `order` — `oldest_first/newest_first`.
Новая задача архивирует прежнюю. Для независимых задач используйте разные
`--data-dir`. Чтобы восстановить повреждённый JSON, сначала сохраните его копию
и проверьте файл `.bak`; приложение не перезаписывает повреждённое состояние.

## Разработка и сборка

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --editable ".[dev,build]"
.\.venv\Scripts\python.exe scripts/provision_tools.py
$env:PLAYWRIGHT_BROWSERS_PATH="0"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m pytest tests
.\.venv\Scripts\python.exe scripts/build_windows.py --version 0.1.0b1 --tag v0.1.0-beta.1
```

Сборка требует Windows x64 Python 3.12. Результат в `release-dist/`: portable ZIP,
build manifest и SHA256. Скрипт проверяет тесты, запуск EXE, версию,
прогресс/экспорт и настоящее окружение Chromium/yt-dlp до упаковки.

`master` — разработка и беты. Ручная stable-команда проверяет последнюю бету,
сливает её в `main`, собирает приложение из `main` и публикует полноценный релиз.
Все release notes краткие, из `release-notes/X.Y.Z.md`.

[Релизный процесс](docs/RELEASES.md) · [Архитектура](docs/ARCHITECTURE.md) ·
[Сторонние компоненты](docs/THIRD_PARTY.md)
