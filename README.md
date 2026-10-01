# Бот приёма статей в журнал

1. Анкета: ФИО → телефон (кнопкой «поделиться контактом» или вручную) → статус автора →
   место работы/учёбы → тема статьи → область науки → язык. В конце автор проверяет данные и может исправить любое поле.
2. Бот показывает реквизиты оплаты (130 000 so'm), автор отправляет чек (фото или PDF).
3. Администратор получает заявку с телефоном и чек с кнопками «✅ TASDIQLASH / ❌ RAD ETISH».
   При подтверждении автору приходит «Chek qabul qilindi, to'lov tasdiqlandi», при отказе он отправляет чек заново.
4. Новую заявку можно подать в любой момент. Кнопка «🆘 Yordam» ведёт в поддержку (@nayimov_82).

Заявки и их статусы хранятся в SQLite (`orders.db`) и сохраняются после перезапуска бота.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # вписать BOT_TOKEN, EDITOR_CHAT_IDS и реквизиты
nohup .venv/bin/python bot.py >> bot.log 2>&1 &
```
Остановка: `pkill -f "Python bot.py"`.

## Команды
- `/start`, `/new`, `/cancel`, `/help`, `/support`
- `/myid` — ID чата
- `/orders` — последние 10 заявок (только для администраторов)

## Деплой на хостинг (Railway и т.п.)

- Тип сервиса — worker (команда запуска в `Procfile`: `python bot.py`), 1 экземпляр, без засыпания.
- Переменные окружения: `BOT_TOKEN`, `EDITOR_CHAT_IDS`, `JOURNAL_NAME`, `PAYMENT_AMOUNT`, `CARD_NUMBER`, `CARD_HOLDER`, `SUPPORT_USERNAME`.
- Подключить volume (например `/data`) и задать `DB_PATH=/data/orders.db`, иначе заявки удалятся при передеплое.
- Перед запуском на сервере остановить локальный бот, иначе Telegram выдаст Conflict.
