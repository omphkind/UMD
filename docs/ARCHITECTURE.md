# Архитектура первой беты 0.1.0

Приложение имеет общий core для консольного интерфейса и будущего GUI.
Windows x64 portable ZIP запускается через `UMD.exe`. Python, Chromium,
локальный yt-dlp и JavaScript runtime входят в сборку.

| Модуль | Ответственность |
| --- | --- |
| `app/core` | Settings, MediaItem, общие ошибки |
| `app/sources` | Source protocol, распознавание URL, YouTube discovery |
| `app/downloader` | Вызов внешнего yt-dlp без shell, таймауты, классификация ошибок |
| `app/localization` | Анонимный Playwright context, локализованные DOM-поля |
| `app/metadata` | Приоритет значений, очистка, нормализация метаданных |
| `app/storage` | Atomic JSON, восстановление после прерывания, lock |
| `app/tasks` | Новая задача, resume, update и retry |
| `app/export` | MetaFin, чистый список URL и JSON |
| `app/ui` | Меню, команды, проверка окружения, понятные сообщения |

MediaItem имеет ключ `source:media_id`, постоянный номер и статус
`pending`, `processing`, `success`, `error` или `skipped`. Состояние хранит
URL источников, снимок настроек задачи, историю и объекты. Сохранение происходит
до и после каждого объекта; `processing` после прерывания возвращается в очередь.
Успешные объекты при resume не обрабатываются повторно. Update добавляет только
новые ID; retry сохраняет ключ и номер существующей ошибки.

Source предоставляет `accepts`, `discover` и `fetch`. Обработчик метаданных
возвращает нормализованные поля, а exporter получает уже обработанные объекты.
Будущие источники и GUI подключаются через эти же сервисы.

В первый MVP входят реальные YouTube-метаданные, локализация, пропуски Shorts
и Members-only, сохранение состояния и экспорт. Полноценная загрузка видео/аудио,
GUI, переводчики, Jellyfin и другие источники остаются следующими этапами,
как указано в задании. Заглушки этих функций в меню отсутствуют.

Для YouTube необходим внешний JS runtime; официальная версия yt-dlp.exe уже
включает EJS. [Документация yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
Chromium упаковывается вместе с Playwright по
[официальной инструкции](https://playwright.dev/python/docs/library#pyinstaller).
