# Архитектура UMD 0.1.0 Beta 2

Приложение имеет общий core для Qt GUI и служебного CLI.
`UMD.exe` открывает GUI без консоли, `UMD-console.exe` предоставляет команды.
Python, Qt, Chromium, локальный yt-dlp, Deno и FFmpeg/FFprobe входят в ZIP.

| Модуль | Ответственность |
| --- | --- |
| `app/core` | Settings, MediaItem, общие ошибки |
| `app/sources` | SourceResolver через реальные extractors yt-dlp, YouTube adapter |
| `app/downloader` | YtDlpDownloader, FFmpegProcessor, форматы, прогресс и прерывание subprocess |
| `app/localization` | Анонимный Playwright context, локализованные DOM-поля |
| `app/metadata` | Приоритет значений, очистка, нормализация метаданных |
| `app/storage` | Atomic JSON, восстановление после прерывания, lock |
| `app/tasks` | Постоянная очередь загрузок; новая обработка метаданных, resume, update и retry |
| `app/export` | MetaFin, чистый список URL и JSON |
| `app/ui` | Qt окно, выбор реальных форматов, CLI и диагностика; без subprocess в виджетах |

MediaItem имеет ключ `source:media_id`, постоянный номер и статус
`pending`, `processing`, `success`, `error` или `skipped`. Состояние хранит
URL источников, снимок настроек задачи, историю и объекты. Сохранение происходит
до и после каждого объекта; `processing` после прерывания возвращается в очередь.
Успешные объекты при resume не обрабатываются повторно. Update добавляет только
новые ID; retry сохраняет ключ и номер существующей ошибки.

Source предоставляет `accepts`, `discover` и `fetch`. Обработчик метаданных
возвращает нормализованные поля, а exporter получает уже обработанные объекты.
Другие источники подключаются через эти же сервисы; отдельные adapters нужны
только для специфичной логики, например локализации YouTube.

SourceResolver возвращает DetectedSource и JSON-совместимый анализ: источник,
extractor, тип, доступность, форматы, субтитры, главы, thumbnail и элементы списка.
DownloadOptions преобразует выбор в format selector; видео/аудио, конвертация,
субтитры, обложки и главы работают через внешний yt-dlp и FFmpeg.

QueueService хранит `UMD_QUEUE.json` отдельно от `UMD_PROGRESS.json`.
Снимок каждой задачи включает настройки, параметры, анализ, файлы и состояние.
Идентификатор загрузки — источник + media ID + параметры; URL-алиасы проверяются
после разрешения extractor-ом. Worker скачивает последовательно, отдельный
worker выполняет Analyze. Qt получает события через таймер и Signal/Slot,
сетевые операции не блокируют event loop. Пауза завершает принадлежащий задаче
subprocess и сохраняет `.part`; продолжение перезапускает yt-dlp с `--continue`.
После сбоя активные задачи становятся Paused. JSON атомарный с резервной копией.

Метаданные канала и MetaFin остаются отдельной обработкой TaskService с постоянными
номерами и экспортом после каждого объекта. GUI использует callbacks того же сервиса.
Переводчики, Jellyfin и AI остаются будущими этапами.

Для YouTube необходим внешний JS runtime; официальная версия yt-dlp.exe уже
включает EJS. [Документация yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
Chromium упаковывается вместе с Playwright по
[официальной инструкции](https://playwright.dev/python/docs/library#pyinstaller).
