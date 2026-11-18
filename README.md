# Kassa Pro — Telegram hisob boti

Yuan va USD o'tkazmalari hisobi: Telegram bot + Mini App. Server'da 24/7 ishlaydi.

## Server sozlamalari (Variables)
| Nom | Qiymat |
|---|---|
| `BOT_TOKEN` | @BotFather bergan token (GitHub'ga **yozilmaydi**) |
| `ADMIN_ID` | Bot egasining Telegram ID si — faqat u bazani yuklay oladi |
| `DB_PATH` | `/data/hisob.db` (Dockerfile'da bor) |
| `PUBLIC_URL` | Ixtiyoriy. Railway/Render'da avtomatik aniqlanadi |

Ma'lumotlar `/data` papkasida saqlanadi — server'da shu papkaga **Volume (disk)** ulang, aks holda qayta ishga tushganda ma'lumot yo'qoladi.

## Eski ma'lumotlarni ko'chirish
Kompyuterdagi `hisob.db` faylini botga Telegram orqali **fayl qilib** yuboring. Bot uni o'zi yuklaydi, eski bazani zaxiraga oladi.
