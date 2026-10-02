# Видео из картинки и петли для сайта: Seedance 2.0 через laozhang (Wan 2.7 по явной просьбе)

Рецепт проверен на живом заказе: блок «1 балл = 1 ₸» на главной странице сайта, бимонетка поворачивается
стороной «1 PT» → «1 ₸» и обратно, бесконечно. Все цифры ниже — оттуда.

## 1. Модели и доступ

| Модель | Группа токена и ключ | Цена | Первый + последний кадр | Заметки |
|---|---|---|---|---|
| Seedance 2.0 standard `doubao-seedance-2-0-260128` | `SeeDance2`, `LAOZHANG_SEEDANCE_API_KEY` | 5 с 720p ≈ $0.90, 1080p ≈ $2.0 | да | **по умолчанию**: движение аккуратнее и детальнее. 480p/720p/1080p, 4–15 с, ratio adaptive/1:1/16:9/9:16/4:3/3:4/21:9, звук выключен |
| Wan 2.7 i2v `wan2.7-i2v` | `Wan`, `LAOZHANG_WAN_API_KEY` | $0.09/с в 720P, $0.15/с в 1080P (5 с 720P = $0.45) | да | **только по явной просьбе**, не как дешёвая замена. 720P/1080P, 2–15 с, пропорции по первому кадру, звук добавляет сам (скрипт срезает) |
| Sora 2 | `Sora2Official` | $0.10/с | только первый | только 16:9 и 9:16; для петель неудобна (скриптом не поддерживается) |
| Kling | на laozhang нет | | | есть на fal.ai и Higgsfield — отдельный аккаунт и оплата |

- Каждой группе нужен свой токен, и тип оплаты у него **по объёму** (pay-as-you-go / 按量). С оплатой за запрос
  laozhang отвечает 503 «no available channels … under billing mode [pay-per-request]» и ничего не списывает.
  Токены создаются в `api2.laozhang.ai/token`; при смене типа оплаты ключ не меняется. Основной ключ группы default
  видео-модели не видит.
- Какие модели видит токен: `GET /v1/models`. Сколько потрачено по токену: `GET /v1/dashboard/billing/usage`
  (`total_usage` в центах). Баланс аккаунта ключом не узнать.
- Токену SeeDance2 могут быть видны и другие версии (например `doubao-seedance-2-5-…`), которых нет в прайсе:
  без проверки цены не брать.
- Seedance списывает в два приёма: предоплата при создании и досчёт по `usage.completion_tokens` в конце.

## Как кадры попадают в модель

Видео-модель работает на чужом сервере и файлы на вашей машине не видит: картинку она скачивает по https-ссылке.

- **Готовая ссылка.** Если кадр уже лежит по https (CDN, бакет, превью сайта) — передайте её в `--first` / `--last` как есть.
- **Своя публичная папка.** Задайте в `~/.config/media-skills/image-gen.env`:
  ```
  IMAGE_GEN_PUBLIC_DIR=/srv/my-site/public/_gen
  IMAGE_GEN_PUBLIC_URL=https://my-site.example/_gen/
  ```
  Тогда `video_generate.py` сам копирует локальный кадр туда под случайным именем (24 hex-символа), проверяет,
  что ссылка открывается (HEAD со своим User-Agent: Cloudflare часто режет стандартный Python-urllib), и удаляет
  файл, когда задача завершилась. Папку настройте без листинга и индексации; кадры в ней публичны на время задачи.
- Без ссылки и без настройки скрипт откажется отправлять локальный файл — денег это не стоит.

## 2. Правила бюджета

1. Сначала кадры через image-gen ($0.03), видео — только после того, как кадр одобрен.
2. Одна платная генерация на задачу, если пользователь не разрешил больше. `video_generate.py` не отправит повторно
   задачу с тем же `--name`; новая генерация = новое имя и явное «да».
3. Перед отправкой показать `--dry-run`: тело запроса и примерную цену.
4. Нет ответа после отправки = могло списаться: не повторять, смотреть `api2.laozhang.ai/log`.
5. Отказ 4xx/503 до генерации не списывается и job-файл не создаёт: исправить причину и отправить снова.

## 3. Кадры для модели

- Пропорции кадра = пропорции ролика. Для блока на сайте удобен квадрат: одно видео и на десктопе, и на телефоне.
- Фон ровный и однотонный, того же цвета, что блок сайта: сгенерировать на ровном фоне, затем
  `bg_recolor.py --color '#hex'`. Объект не шире ~60% кадра, вокруг чистый фон под мягкую маску края.
- Объект должен отделяться от фона блока. Оранжевая монета на оранжевом блоке пропадала бы; хромированное кольцо
  биметаллической монеты это решило.
- Последний кадр делать правкой первого: `generate.py --ref <первый кадр>`, в промпте «тот же кадр, тот же ракурс,
  размер, свет и фон», меняется только нужное. Положение объекта сверить по bbox, который печатает `bg_recolor.py`.
- Короткие надписи на объекте («1 PT», «1 ₸») обе модели держат без искажений при повороте, если заданы оба кадра.

## 4. Промпт

Описывать переход от первого кадра к последнему, неподвижную камеру и запреты. Сработал в обеих моделях:

```text
A thick bimetallic coin floats in the center of a flat, uniform bright orange background. The coin slowly and smoothly
turns half a revolution around its vertical axis: the side with the embossed "1 PT" turns away and the reverse side with
the embossed "1 ₸" comes into view, ending exactly in the pose of the final frame. Physically correct rigid 3D rotation of
a solid metal coin; the polished chrome ring and the knurled edge catch the light as it turns. The coin stays in place
and keeps its size. Static locked-off camera, no zoom, no camera movement. The flat orange background stays perfectly
uniform and unchanged. No morphing, no melting, no extra objects, no particles, no text other than on the coin.
```

`--negative`: `morphing, melting, warping, distorted letters, changing text, extra coins, duplicate coin, hands, camera
movement, zoom, background gradient, vignette, shadow, flicker, particles, blur, watermark`. Wan берёт его отдельным полем,
Seedance получает дописанным в текст.

## 5. Как зациклить

- **Переход A→B и обратно, покачивание, полуоборот**: одна генерация (первый A, последний B) и
  `video_loop.py --mode pingpong`. При неподвижном свете обратный ход физически правдоподобен.
  - Seedance двигает объект с первого кадра: нужен `--ease 12`, иначе на развороте рывок.
  - Wan держит у краёв почти секунду стоянки: вместо `--ease` подрезать `--trim 10 139`.
- **Непрерывное вращение в одну сторону**: две генерации (A→B и B→A по тем же кадрам) и склейка без двойного кадра.
  Вдвое дороже — только с согласия пользователя.
- **Одинаковые первый и последний кадр**: модели часто почти не двигают объект. Годится для «дыхания» света, не для поворота.
- `video_loop.py` печатает шов: разницу последнего кадра петли с первым относительно обычного шага. До 1.5 стыка не видно.

## 6. Качество и сжатие

- Сырой ролик модели не пережимать повторно до финала: `video_loop.py` собирает мастер без потерь и кодирует из него.
- Размер файла = ширина показа на десктопе × 2 (Retina). Модель в 720p даёт 960×960; при показе 640 CSS px это 1280
  физических пикселей, поэтому ролик заранее увеличивается lanczos до 1280: браузерное растягивание мыльнее.
- Основной AV1 (SVT-AV1 CRF 36), запасной H.264 (CRF 24), в двух `<source>`: браузер берёт первый, который умеет.
  Замер на монете, 11 с, 1280²: AV1 CRF 36 = 1.7 МБ, H.264 CRF 24 = 2.6 МБ, HEVC CRF 26 = 2.3 МБ, H.264 CRF 20 = 4.1 МБ.
  По кропу 1:1 AV1 CRF 36 почти не отличим от исходника; HEVC третьим файлом почти ничего не даёт.
- **Ошибка, которую уже совершали**: H.264 960 px CRF 32 (0.76 МБ) съел около четверти детализации — пятна на
  градиентах и грязь у граней. На десктопе это видно сразу.
- Матрица RGB→YUV задаётся явно BT.709 под метку файла, иначе браузер сдвигает цвет фона видео относительно
  CSS-заливки. После кодирования фон отличается от hex на 2–5 единиц; мягкая маска края это прячет.
- Проверять глазами кроп 1:1 рядом с исходником модели: SSIM пятен не ловит.
- **GIF не использовать.** Та же петля в GIF даже в 640 px весит 22.4 МБ и ограничена 256 цветами (полосы на
  градиентах), анимированный WebP 640 px весит 4.9 МБ. Видео декодирует видеочип; Lighthouse штрафует GIF-анимацию.

## 7. Встраивание на сайт

HTML печатает `video_loop.py`:

```html
<video class="loop-media" data-lazy-video muted playsinline loop preload="none" aria-hidden="true"
  disablepictureinpicture disableremoteplayback poster="/assets/coin-poster.<hash>.webp" width="1280" height="1280">
  <source data-src="/assets/coin-av1.<hash>.mp4" type="video/mp4; codecs=av01.0.08M.08">
  <source data-src="/assets/coin-h264.<hash>.mp4" type="video/mp4">
</video>
```

CSS: фон блока = фон кадра, край видео мягко растворяется:

```css
.loop-media{display:block;width:100%;height:auto;-webkit-mask-image:radial-gradient(closest-side,#000 74%,transparent 100%);mask-image:radial-gradient(closest-side,#000 74%,transparent 100%)}
```

JS: файл грузится, только когда блок подъезжает к экрану, играет только на экране. При отключённой анимации,
экономии трафика и в энергосбережении iOS (play() отклонён) остаётся постер:

```js
const videos = [...document.querySelectorAll('video[data-lazy-video]')];
const reduce = matchMedia('(prefers-reduced-motion: reduce)');
const saveData = navigator.connection?.saveData === true;
const visible = new Set();
function sync(video) {
  if (!visible.has(video) || reduce.matches || saveData) { video.pause(); return; }
  const pending = video.querySelectorAll('source[data-src]');
  if (pending.length) { pending.forEach(s => { s.src = s.dataset.src; s.removeAttribute('data-src'); }); video.load(); }
  video.play().catch(() => {});
}
const io = new IntersectionObserver(entries => entries.forEach(e => {
  e.isIntersecting ? visible.add(e.target) : visible.delete(e.target);
  sync(e.target);
}), { rootMargin: '160px 0px' });
videos.forEach(v => io.observe(v));
reduce.addEventListener('change', () => videos.forEach(sync));
```

- Постер `video_loop.py` делает из первого кадра петли: его и видит человек без анимации.
- Хеш в имени файлов обязателен: статика обычно кэшируется сервером и CDN, без хеша посетители долго видят старую версию.
- Сервер должен отдавать `video/mp4` и запросы Range (206), иначе Safari не играет. Caddy `file_server` и nginx умеют.
- Стили внутри блока писать с прямыми потомками (`.block>small`), иначе они цепляют чужие элементы внутри блока
  (например, подписи у кнопок сторов).
- Крупный текст на ярком оранжевом `#ff5715` только белый `#fff` (3.17:1). Кремовый `#fff4df` даёт 2.9:1 и роняет
  Accessibility в PageSpeed.

## 8. Проверка

- Браузерный E2E (Playwright или аналог): видео не скачивается до подъезда к экрану; у экрана играет
  (`paused=false`, `currentTime` растёт); Chrome выбрал AV1 (`currentSrc`); вне экрана пауза; угол кадра через canvas
  отличается от заливки блока не больше чем на 8; при reduced-motion постер без загрузки; ширины 1440 / 390 / 360
  без горизонтальной прокрутки.
- PageSpeed Insights: `pagespeed.web.dev/analysis?url=…&form_factor=mobile`. API без ключа часто отвечает 429 из-за общей
  квоты. Ориентир с таким блоком: телефон 98, компьютер 100, TBT 0, CLS 0; до подъезда к блоку скачивается только постер (~60 КБ).
- Если физического iPhone и Safari под рукой нет — так и говорить, а не утверждать, что «на iPhone работает».

## 9. Весь путь командами

```bash
SKILL=/abs/path/to/image-gen; PY="$SKILL/.venv/bin/python"
# 1. кадры: первый и последний (правкой первого), $0.03 каждый
"$PY" "$SKILL/generate.py" --prompt "…" --ratio 1:1 --keep-2k --ref <референс> --name coin_side_a --project клиент/дата-слаг
"$PY" "$SKILL/generate.py" --prompt "Тот же кадр … меняется только …" --ratio 1:1 --keep-2k --ref <кадр A> --name coin_side_b --project клиент/дата-слаг
# 2. фон кадров = цвет блока сайта
"$PY" "$SKILL/bg_recolor.py" <кадр A> first.png --color '#ff5715' --size 1280
"$PY" "$SKILL/bg_recolor.py" <кадр B> last.png --color '#ff5715' --size 1280
# 3. одна генерация: показать --dry-run, после согласия запустить без него, затем забрать результат
"$PY" "$SKILL/video_generate.py" create --name клиент-объект --first first.png --last last.png \
    --prompt "…" --negative "…" --duration 5 --resolution 720p --project клиент/дата-слаг --dry-run
"$PY" "$SKILL/video_generate.py" poll --name клиент-объект
# 4. петля и файлы для сайта
"$PY" "$SKILL/video_loop.py" <raw.mp4> --name coin --out-dir <сайт>/public/assets --mode pingpong --ease 12 --bg '#ff5715' --size 1280
```

Где лежит (база `IMAGE_GEN_DATA_DIR`, по умолчанию `~/.local/share/image-gen`): журнал запросов
`<база>/video-gen/ledger.jsonl`, задачи `<база>/video-gen/jobs/<имя>.json`, ролики
`<база>/creatives/<project>/videos/<имя>/raw.mp4` (без `--project` — `<база>/video-gen/videos/<имя>/`).
Для `video_loop.py` нужен `ffmpeg` с `libsvtav1` и `libx264`.
