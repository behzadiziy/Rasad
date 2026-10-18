## الگوی کلی برای هر Task

هر Task همین هفت مرحله را دارد:

**۱. شروع تمیز.** Task قبلی باید commit شده باشد. با `git status` چک کنید که چیزی باقی نمانده باشد.

**۲. session جدید.** در Claude Code، `/clear` را بزنید و با `Shift+Tab` به حالت plan بروید.

**۳. پیام شروع.** این قالب را بفرستید:

```
Read tasks/<file>.md and execute Task <N>.

Notes from earlier tasks:
- <any finding this task needs that isn't in the repo yet>
```

بخش «Notes» فقط وقتی لازم است که Task قبلی چیزی پیدا کرده باشد که فقط در گزارش آمده و در فایل‌های پروژه ثبت نشده است. مثال قبلی‌اش این بود که API جستجوی JobVision درخواست POST به `candidateapi` است. اگر چنین یافته‌ای نیست، این بخش را حذف کنید.

**۴. بررسی پلن.** قبل از تأیید، این موارد را چک کنید:
- فقط فایل‌هایی که در Deliverables و Authorized Changes آمده‌اند تغییر می‌کنند.
- وابستگی جدیدی اضافه نمی‌شود. فقط Task 7 اجازه تغییر `pyproject.toml` را دارد.
- فرض‌های پلن منطقی‌اند.

اگر خواستید، پلن را برای من بفرستید تا بررسی کنیم.

**۵. بررسی گزارش Self-Review.** این موارد را چک کنید:
- هر علامت ⚠️ دلیل روشنی دارد.
- فهرست «Changes to Frozen Files» دقیقاً با بخش Authorized Changes همان Task یکی است.
- دستورهایی که بخش «How to Verify» به شما سپرده را اجرا کنید.

**۶. اصلاح قبل از commit.** اگر مشکلی دیدید، در همان session و بدون `/clear` اصلاحش کنید.

**۷. commit:**

```bash
git status    # config/master_resume.yaml, inputs/ and data/ must not appear
git add -A
git commit -m "Task <N>: <title>"
```

**commit نکنید اگر:** تست‌ها شکست خورده‌اند، فایلی خارج از فهرست مجاز تغییر کرده، یا اطلاعات شخصی در fixtureها یا لاگ‌ها دیده می‌شود.

## نکات مخصوص هر Task

**Task 7 — زیرساخت نسخه ۲**
- **قبل از شروع:** کاری لازم نیست. خود Claude Code نام مدل Gemini را از مستندات رسمی بررسی می‌کند.
- **بعد از اتمام:** Claude Code اجرای `--reevaluate --dry-run` را خودش روی کپی پایگاه داده و روی خود پایگاه داده انجام می‌دهد. کاری برای شما باقی نمی‌ماند.

**Task 7b — کیفیت تطبیق و آگهی‌های تکراری**
- **بعد از اتمام:** خروجی `--reevaluate --dry-run` را خودتان بخوانید. `exclude_keywords` و `description_min_hits` را آن‌قدر تنظیم کنید و dry-run را تکرار کنید تا نتیجه درست شود. بعد اجرای واقعی را بزنید:

```bash
uv run python main.py --reevaluate
```

**Task 8 — رزومه مرجع و موتور تنظیم رزومه**
- **قبل از شروع:**
  - فایل Word رزومه را در `inputs/` بگذارید (هر دو زبان، اگر دارید).
  - `GEMINI_API_KEY` و در صورت نیاز `LLM_PROXY_URL` را در `config/.env` وارد کنید.
- **وسط کار:** Claude Code بعد از ساختن `config/master_resume.yaml` متوقف می‌شود. این فایل را دقیق بازبینی کنید: سوابق، تاریخ‌ها، و هر بخشی که علامت `# translated — review` دارد. بعد بگویید «reviewed, continue».
- **بعد از اتمام:** تنظیم رزومه را برای یک آگهی فارسی و یک آگهی انگلیسی اجرا کنید. این اجرا هزینه API دارد.

```bash
uv run python -m src.resume.tailor --job <platform>:<id> --save
```

**Task 9 — ساخت PDF**
- **قبل از شروع:** حداقل دو رکورد ذخیره‌شده از Task 8 باید وجود داشته باشد.
- **بعد از اتمام:** PDFها را باز کنید و این موارد را چک کنید:
  - راست‌به‌چپ بودن، فونت، و تاریخ‌های شمسی در نسخه فارسی
  - اینکه هایلایت‌های نسخه بازبینی با خلاصه تغییرات Task 8 یکی باشد
  - اینکه متن نسخه تمیز قابل انتخاب باشد

**Task 10 — ربات تلگرام دوطرفه**
- **بعد از اتمام:**
  1. در چت خصوصی با ربات `/start` را بفرستید. ربات شناسه عددی شما را برمی‌گرداند. آن را در `TELEGRAM_ALLOWED_USER_IDS` بگذارید.
  2. ربات را اجرا کنید:

     ```bash
     uv run python main.py --bot
     ```

  3. دستور `/tailor` و هر سه دکمه را آزمایش کنید.
  4. یک بار هم با یک حساب تلگرام دیگر دکمه‌ای را بزنید تا مطمئن شوید رد می‌شود.

**Task 11 — شناسایی فرم درخواست**
- **قبل از شروع:** در هر سایت یک آگهی انتخاب کنید که واقعاً می‌خواهید برایش درخواست بفرستید.
- **وسط کار:** Claude Code بعد از ساختن اسکریپت متوقف می‌شود. ورود و ضبط را خودتان انجام دهید:

```bash
uv run python -m scripts.apply_probe --platform jobinja --login
uv run python -m scripts.apply_probe --platform jobinja --job-url "<job URL>"
```

  همین دو دستور را برای `jobvision` هم اجرا کنید. بعد بگویید «captures done».
- **بعد از اتمام:** `docs/apply_flows.md` را بخوانید، به‌خصوص بخش «Resume choice».

**Task 12 — ارسال خودکار درخواست**
- **بعد از اتمام:**
  1. dry-run را برای هر سایت اجرا کنید. چیزی ارسال نمی‌شود:

     ```bash
     uv run python -m src.apply.jobinja --resume-id <N> --dry-run --headful
     ```

  2. در config، `apply.enabled: true` بگذارید. برای اولین درخواست واقعی `confirm_before_submit: true` را هم روشن کنید.
  3. یک آگهی واقعی را تأیید کنید و منتظر رسید بمانید.

## یک پیشنهاد اختیاری

گزارش Self-Review یافته‌هایی دارد که Task بعدی لازمشان دارد، و الان باید خودتان آن‌ها را در بخش «Notes» کپی کنید. اگر این خط را به انتهای بخش Task Workflow در `CLAUDE.md` اضافه کنید، Claude Code خودش این یافته‌ها را جدا می‌کند:

```
5. End the Self-Review with **Notes for Later Tasks**: findings that a later task will need and that aren't recorded in any file (e.g., an API route discovered from a capture), or "None".
```

بعد کافی است همان بخش را در پیام شروع Task بعدی بگذارید.