<p align="center">
  <img src="images/readme-banner.svg" width="100%" alt="Telegram Music Bot — общая музыка в любом чате">
</p>

<p align="center">
  <a href="https://github.com/andergr0ynd/telegram-music-bot/archive/refs/heads/main.zip"><img src="images/download.svg" width="243" alt="Скачать ZIP для Windows"></a>
  &nbsp;
  <a href="https://github.com/andergr0ynd/telegram-music-bot/issues"><img src="images/feedback.svg" width="243" alt="Открыть обсуждения в GitHub Issues"></a>
</p>

<p align="center">
  <strong>Windows · Python 3.10+ · Telegram</strong><br>
  Отправь боту аудио в личке, найди его по названию и поделись им в любом чате.
</p>

<p align="center">
  <a href="#install">Установка</a> &nbsp;·&nbsp;
  <a href="#features">Возможности</a> &nbsp;·&nbsp;
  <a href="#commands">Команды</a> &nbsp;·&nbsp;
  <a href="#faq">Частые вопросы</a>
</p>

---

<a id="install"></a>

## Установка за три шага

**1. Скачай [ZIP](https://github.com/andergr0ynd/telegram-music-bot/archive/refs/heads/main.zip) и распакуй его.** Установи Python 3.10 или новее. Дополнительные пакеты не нужны.

**2. Настрой бота в [@BotFather](https://t.me/BotFather).** Создай бота, переименуй копию [.env.example](.env.example) в `.env` и вставь токен вместо `PASTE_TOKEN_HERE`. Затем отправь в BotFather `/setinline`, выбери своего бота и задай подсказку «Найти музыку».

**3. Открой [start.bat](start.bat).** Нажми «Старт» у своего бота и отправь MP3 или M4A как аудио с плеером. Когда бот подтвердит готовность, введи в любом чате `@имя_бота название` и выбери трек.

> [!TIP]
> Поставь пробел после имени бота. Пустой запрос покажет последние треки.

<a id="features"></a>

## Что умеет бот

| Возможность | Как работает |
| :--- | :--- |
| **Общая библиотека** | Все пользователи могут добавлять аудио и искать среди общих загрузок. Одинаковые треки объединяются в выдаче. |
| **Поиск из любого чата** | Ищи по названию, исполнителю, имени файла или подписи. Аудио отправляется в чат после выбора результата. |
| **Локальные копии** | Бот сохраняет файлы в папку `music`, а метаданные — в SQLite. |
| **Восстановление аудио** | Бот повторно загружает твои треки из локальной копии и проверяет их для inline. |

<a id="commands"></a>

## Команды в личке

| Команда | Действие |
| :--- | :--- |
| `/search название` | Найти треки в общей библиотеке |
| `/library` | Показать последние 20 общих треков |
| `/cache` | Скачать свои треки в папку `music` |
| `/repair` | Повторно подготовить свои треки для inline |
| `/help` | Открыть подсказку |

<a id="faq"></a>

## Частые вопросы

<details>
<summary><strong>Почему бот не появляется при вводе имени?</strong></summary>

Включи inline через `/setinline` в BotFather и введи имя бота с пробелом. Компьютер с ботом должен быть включён и подключён к интернету. При ошибке 409 закрой второй экземпляр программы с тем же токеном.

</details>

<details>
<summary><strong>Почему трек не отправляется из поиска?</strong></summary>

В личных сообщениях с ботом отправь `/repair`, если это твоя загрузка. Бот возьмёт файл из локальной копии или скачает его снова. Через обычный Telegram Bot API бот скачивает файлы до 20 МБ.

</details>

<details>
<summary><strong>Что сохранить для резервной копии?</strong></summary>

Скопируй папку `music` и базу `library_<bot_id>.sqlite3`. Пути внутри музыкальной библиотеки управляются ботом. Чтобы добавить новый трек, отправь его в личку.

</details>

## Проверка

```powershell
python -m unittest -v
```

---

[**Сообщить об ошибке →**](https://github.com/andergr0ynd/telegram-music-bot/issues/new) · [Поиск по транслиту и с опечатками](https://github.com/andergr0ynd/telegram-music-bot/issues/1)
