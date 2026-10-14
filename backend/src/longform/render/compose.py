"""Слой кадра при сборке: экран, лицо, экран с лицом в углу, экран в рамке
с лицом сбоку, крупный план лица и движение камеры (смена плана на склейках,
укрупнение с плавным возвратом, медленный наезд), приближение области
экрана, шторка.

Экран собирается как раньше (render/select.py): поток [S] — кадры записи
экрана на сетке выходного ролика. Камера пересчитывается во время записи
экрана: кадр камеры с номером j стоит в момент
    main = (j / fps_камеры − смещение) / (1 + уплывание),
затем фильтр fps приводит её к частоте ролика (round=up: в кадре ролика —
тот кадр камеры, который виден в этот момент, как frame_at), и к ней
применяется тот же выбор кадров, что к экрану. Так поток [C] идёт кадр в
кадр с [S]: вырезы и стыки у камеры те же сами собой.

Дальше ролик режется на участки с одной раскладкой (Shot), каждый участок
собирается отдельно, участки склеиваются. Номера кадров и звук от этого не
меняются: раскладка — только картинка.
"""

from dataclasses import dataclass, replace
from fractions import Fraction

from longform.edl import PlacedClip
from longform.probe import ProbeResult

ZOOM_RAMP_S = Fraction(2, 5)  # подвод и возврат приближения, секунды
DROP_RAMP_S = Fraction(2, 5)  # опускание и подъём экрана в рамке (side, drop), секунды
PUSH_MIN_S = 5  # медленный наезд — только на кусках камеры не короче, секунды
ALT_MIN_S = 1  # смена плана: кусок короче остаётся в плане предыдущего, секунды
PIP_MARGIN = Fraction(1, 40)  # отступ лица в углу от края — доля ширины
SLOTS = ("br", "bl", "tr", "tl")
WHIP_S = Fraction(1, 5)  # половина шторки: столько уезжает старое и столько же приходит новое

# Экран в рамке, лицо сбоку — размеры по умолчанию для кадра 2560×1440,
# дальше пересчитываются под размер ролика.
SIDE_REF = (2560, 1440)
SIDE_BG = "0x1b1b1b"  # фон: почти чёрный, с еле заметной размытой сеткой
SIDE_GRID = "0x2a2a2a"
SIDE_BORDER = 9  # белая рамка лица
SIDE_RADIUS = 26  # скругление углов экрана
# Экран целиком (слайды, приложение на весь экран): масштаб 0,879, слева сверху
SIDE_SCREEN = {"card": (83, 87, 2250, 1266), "face": (2061, 922, 463)}
# Область экрана (окно приложения): крупнее, лицо больше и справа от окна
SIDE_WINDOW = {"card_xy_h": (32, 64, 1283), "face": 711, "face_gap": 4, "face_drop": 14}


@dataclass(frozen=True)
class CameraSource:
    """Камера, сведённая с записью экрана: cam_us = offset_us + main_us × (1 + drift_ppm / 1e6)."""

    asset_id: str
    offset_us: int
    drift_ppm: float


@dataclass(frozen=True)
class Shot:
    """Участок выходного ролика [start, end) кадров с одной раскладкой."""

    start: int
    end: int
    layout: str  # screen | camera | pip | side
    zoom: tuple[float, float, float, float] | None = None  # область экрана: x, y, w, h (доли кадра)
    punch: tuple[float, float, float] | None = None  # крупный план лица: масштаб, центр x, центр y (доли)
    pip_slot: str = "br"
    pip_size: float = 0.22  # ширина лица в углу — доля ширины кадра
    region: tuple[float, float, float, float] | None = None  # side: какая часть экрана в рамке (доли)
    face_cx: float = 0.5  # side: центр квадрата лица по ширине кадра камеры (доля)
    enter: str | None = None  # переход на участок: whip — вертикальная шторка
    # camera: укрупнение (punch) за столько секунд плавно возвращается к общему плану
    punch_release: float | None = None
    # camera: зум — лицо плавно приближается до punch за столько секунд от
    # начала участка (без него — рывком); с punch_release потом плавно назад
    punch_attack: float | None = None
    # camera: медленный наезд на каждом куске между склейками — на столько
    # крупнее к концу куска (0.04 — на 4 %); куски короче PUSH_MIN_S стоят
    push: float | None = None
    # camera: смена плана на склейках — куски между склейками по
    # очереди общий план и крупнее: (на сколько крупнее, центр x, центр y)
    alternate: tuple[float, float, float] | None = None
    cuts: tuple[int, ...] = ()  # склейки внутри участка: кадры от его начала
    # side: экран в рамке плавно опускается на эту долю высоты кадра и в конце
    # участка возвращается — сверху освобождается место
    drop: float | None = None
    # Шторка у этого участка, если он собирается отдельно от соседей
    # (render/select.py, сборка по участкам): None — считать по соседям в плане
    whip_in: bool | None = None
    whip_out: bool | None = None
    # Пробный ролик с середины участка: столько кадров участка прошло до его
    # начала, всего в участке span кадров. Движение камеры считается от
    # настоящего начала участка — как в готовом ролике
    skip: int = 0
    span: int | None = None
    # broll: свой кадр во весь экран — путь к клипу и с какой секунды клипа
    # (голос идёт поверх: звук ролика собирается отдельно)
    clip: str | None = None
    clip_at: float = 0.0
    # side: видео-фон вместо тёмной сетки (идёт по кругу) и с какой секунды
    # фона начать участок — фон течёт непрерывно через весь ролик
    bg: str | None = None
    bg_at: float = 0.0
    # Положение, которое автор выставил мышью на мониторе (как Motion в Premiere),
    # доли кадра: окно лица (x, y — левый верхний угол; size — ширина) у side и
    # pip, рамка экрана (x, y, ширина; высота — по пропорции экрана) у side
    face_box: tuple[float, float, float] | None = None
    card_box: tuple[float, float, float] | None = None

    @property
    def uses_camera(self) -> bool:
        return self.layout in ("camera", "pip", "side")

    @property
    def full_frames(self) -> int:
        return self.span or self.end - self.start


@dataclass(frozen=True)
class VisualPlan:
    width: int
    height: int
    shots: tuple[Shot, ...]
    cameras: dict  # id записи экрана -> CameraSource

    @property
    def uses_camera(self) -> bool:
        return any(s.uses_camera for s in self.shots)

    def slice(self, out_start: int, out_end: int) -> "VisualPlan":
        """План части ролика [out_start, out_end) — для пробного ролика по
        части монтажа (CompiledEdl.slice). Участки, начатые до неё, помнят,
        сколько кадров прошло; шторка — только у участков, видных от начала
        или до конца."""
        shots = []
        for i, s in enumerate(self.shots):
            lo, hi = max(s.start, out_start), min(s.end, out_end)
            if lo >= hi:
                continue
            head = s.whip_in if s.whip_in is not None else (s.enter == "whip" and i > 0)
            tail = s.whip_out if s.whip_out is not None else (
                i + 1 < len(self.shots) and self.shots[i + 1].enter == "whip")
            shots.append(replace(s, start=lo - out_start, end=hi - out_start, skip=s.skip + lo - s.start,
                                 span=s.full_frames, whip_in=head and lo == s.start, whip_out=tail and hi == s.end))
        return VisualPlan(self.width, self.height, tuple(shots), self.cameras)

    def is_plain(self, frame: int) -> bool:
        """Экран как есть (без лица и приближения) в этом кадре ролика."""
        shot = next((s for s in self.shots if s.start <= frame < s.end), None)
        return shot is not None and shot.layout == "screen" and shot.zoom is None


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def camera_input_args(cam: ProbeResult, source: CameraSource, run_: list[PlacedClip], fps: Fraction) -> tuple[list[str], int]:
    """Перемотка камеры к кадру, видимому за секунду до начала серии, и предел
    чтения. Возвращает аргументы входа и номер первого раскодированного кадра."""
    from longform.timebase import frame_at

    first, last = run_[0].src_start_frame, run_[-1].src_end_frame
    scale = 1 + source.drift_ppm / 1e6
    start_us = source.offset_us + float(first / fps) * 1e6 * scale - 1_000_000
    j0 = min(max(0, frame_at(int(start_us), cam.video.fps)), cam.video.frame_count - 1)
    args = []
    if j0 > 0:
        t = cam.video.start + Fraction(2 * j0 - 1, 2) / cam.video.fps - cam.container_start
        args = ["-ss", f"{float(t):.6f}"]
    span = float((last - first) / fps) * scale + 2
    return [*args, "-t", f"{span:.6f}"], j0


def camera_chain(label_in: str, label_out: str, cam: ProbeResult, source: CameraSource, j0: int, first: int,
                 fps: Fraction, select_expr: str, run_frames: int) -> str:
    """Кадры камеры на сетке серии экрана, тот же выбор кадров, что у экрана."""
    cf = cam.video.fps
    scale = 1 + source.drift_ppm / 1e6
    # Время кадра камеры в секундах записи экрана, от первого кадра серии
    pts = (f"(({j0}+N)*{cf.denominator}/{cf.numerator}-{source.offset_us / 1e6:.9f})/{scale:.12f}"
           f"-{first}*{fps.denominator}/{fps.numerator}")
    return (
        f"{label_in}setpts=({pts})/TB,"
        f"fps=fps={fps.numerator}/{fps.denominator}:start_time=0:round=up,"
        # Камера могла кончиться раньше серии: дальше повторяем последний кадр,
        # а участки с лицом вне записи камеры отсекает план (visual.py).
        # Только конечное продление: с бесконечным ffmpeg не завершается
        f"tpad=stop_mode=clone:stop={run_frames},"
        f"select='{select_expr}',trim=end_frame={run_frames},"
        f"settb={fps.denominator}/{fps.numerator},setpts=N{label_out}"
    )


def _fit(w: int, h: int) -> str:
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def _fill(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def _zoom(shot: Shot, w: int, h: int, fps: Fraction) -> str:
    """Подвод к области — неподвижное чтение — возврат (zoompan, d=1)."""
    x, y, rw, rh = shot.zoom
    z = min(1 / rw, 1 / rh)
    cx, cy = x + rw / 2, y + rh / 2
    dur = (shot.end - shot.start) / fps
    ramp = float(min(ZOOM_RAMP_S, dur / 3))
    # p — доля приближения: плавно 0→1 в начале участка и 1→0 в конце
    q = f"min(1,min(it/{ramp:.6f},({float(dur):.6f}-it)/{ramp:.6f}))"
    p = f"({q})*({q})*(3-2*({q}))"
    return (f"{_fit(w, h)},zoompan=z='1+{z - 1:.6f}*{p}':"
            f"x='max(0,min(iw-iw/zoom,(0.5+{cx - 0.5:.6f}*{p})*iw-iw/zoom/2))':"
            f"y='max(0,min(ih-ih/zoom,(0.5+{cy - 0.5:.6f}*{p})*ih-ih/zoom/2))':"
            f"d=1:s={w}x{h}:fps={fps.numerator}/{fps.denominator}")


def _punch(shot: Shot, w: int, h: int) -> str:
    s, cx, cy = shot.punch
    return (f"crop=iw/{s:.6f}:ih/{s:.6f}:"
            f"max(0\\,min(iw-iw/{s:.6f}\\,{cx:.6f}*iw-iw/{s:.6f}/2)):"
            f"max(0\\,min(ih-ih/{s:.6f}\\,{cy:.6f}*ih-ih/{s:.6f}/2)),{_fill(w, h)}")


def alternate_levels(shot: Shot, fps: Fraction) -> list[tuple[int, int, bool]]:
    """Куски участка между склейками и план каждого: [(начало, конец, крупный)].
    Первый кусок — общий план, дальше план меняется на каждой склейке; кусок
    короче ALT_MIN_S остаётся в плане предыдущего (не мелькает туда-обратно)."""
    frames = shot.full_frames
    bounds = [0, *sorted(c for c in shot.cuts if 0 < c < frames), frames]
    out, tight = [], False
    for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
        if k and b - a >= ALT_MIN_S * fps:
            tight = not tight
        out.append((a, b, tight))
    return out


def _camera_motion(shot: Shot, fps: Fraction) -> str | None:
    """Движение лица во весь кадр: смена плана на склейках, зум (плавное
    приближение), укрупнение с плавным возвратом и медленный наезд по кускам. perspective берёт рамку с
    точностью до доли пикселя (zoompan округляет её до двух пикселей —
    медленный наезд дрожит).
    Номер кадра в участке — in − 1: счётчик фильтра начинается с единицы."""
    n = f"(in-1+{shot.skip})" if shot.skip else "(in-1)"
    levels = alternate_levels(shot, fps) if shot.alternate else []
    if not (shot.push or (shot.punch and (shot.punch_release or shot.punch_attack)) or any(t for *_, t in levels)):
        return None
    s, cx, cy = shot.punch or (1.0, 0.5, 0.5)
    terms = []
    if shot.alternate:
        step, cx, cy = shot.alternate
        # Соседние куски одного плана — одно условие: выражение короче
        runs = [(b, t) for (_, b, t), nxt in zip(levels, [*levels[1:], None]) if nxt is None or nxt[2] != t]
        expr = "1"
        for b, tight in reversed(runs):
            expr = f"if(lt({n},{b}),{1 + step if tight else 1:.6f},{expr})"
        terms.append(expr)
    if shot.punch and (shot.punch_release or shot.punch_attack):
        smooth = lambda p: f"({p})*({p})*(3-2*({p}))"  # noqa: E731 — плавно в начале и в конце
        a = round(Fraction(str(shot.punch_attack)) * fps) if shot.punch_attack else 0
        up = smooth(f"min(1,{n}/{a})") if a else "1"
        if shot.punch_release:
            r = max(1, round(Fraction(str(shot.punch_release)) * fps))
            down = smooth(f"min(1,max(0,({n}-{a})/{r}))")
        else:
            down = "0"
        terms.append(f"(1+{s - 1:.6f}*({up}-{down}))")
    elif shot.punch:
        terms.append(f"{s:.6f}")
    if shot.push:
        frames = shot.full_frames
        bounds = [0, *sorted(c for c in shot.cuts if 0 < c < frames), frames]
        min_len = PUSH_MIN_S * fps
        expr = "1"
        for a, b in reversed(list(zip(bounds, bounds[1:]))):
            piece = f"(1+{shot.push:.6f}*({n}-{a})/{b - a})" if b - a >= min_len else "1"
            expr = f"if(lt({n},{b}),{piece},{expr})"
        terms.append(expr)
    z = "*".join(terms)
    x0 = f"max(0,min(W-W/({z}),{cx:.6f}*W-W/({z})/2))"
    y0 = f"max(0,min(H-H/({z}),{cy:.6f}*H-H/({z})/2))"
    x1, y2 = f"{x0}+W/({z})", f"{y0}+H/({z})"
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y2}':"
            f"x3='{x1}':y3='{y2}':interpolation=linear:eval=frame")


def pip_geometry(shot: Shot, w: int, h: int, cam_w: int, cam_h: int) -> tuple[int, int, int, int]:
    if shot.face_box:
        fx, fy, size = shot.face_box
        pw = _even(w * size)
        return _even(fx * w), _even(fy * h), pw, _even(pw * cam_h / cam_w)
    pw = _even(w * shot.pip_size)
    ph = _even(pw * cam_h / cam_w)
    m = _even(w * float(PIP_MARGIN))
    x = w - pw - m if shot.pip_slot in ("br", "tr") else m
    y = h - ph - m if shot.pip_slot in ("br", "bl") else m
    return x, y, pw, ph


def side_geometry(shot: Shot, w: int, h: int, scr_w: int, scr_h: int) -> dict:
    """Рамка экрана и квадрат лица в кадре w×h: {card: (x, y, cw, ch), face: (x, y, size), border, radius}."""
    kx, ky = w / SIDE_REF[0], h / SIDE_REF[1]
    rx, ry, rw, rh = shot.region or (0.0, 0.0, 1.0, 1.0)
    aspect = (rw * scr_w) / (rh * scr_h)
    border = max(2, round(SIDE_BORDER * kx))
    radius = max(2, round(SIDE_RADIUS * kx))
    if aspect >= 1.6:
        x, y, cw, ch = SIDE_SCREEN["card"]
        # Экран другой пропорции вписываем в ту же рамку по центру
        fw, fh = cw, cw / aspect
        if fh > ch:
            fw, fh = ch * aspect, ch
        card = (_even((x + (cw - fw) / 2) * kx), _even((y + (ch - fh) / 2) * ky), _even(fw * kx), _even(fh * ky))
        fx, fy, fs = SIDE_SCREEN["face"]
        face = (_even(fx * kx), _even(fy * ky), _even(fs * kx))
    else:
        x, y, ch = SIDE_WINDOW["card_xy_h"]
        fs = SIDE_WINDOW["face"]
        cw = ch * aspect
        # Окно с лицом справа должны поместиться в кадр
        room = SIDE_REF[0] - x - SIDE_WINDOW["face_gap"] - fs - x
        if cw > room:
            cw, ch = room, room / aspect
        card = (_even(x * kx), _even(y * ky), _even(cw * kx), _even(ch * ky))
        face_y = y + ch + SIDE_WINDOW["face_drop"] - fs
        face = (_even((x + cw + SIDE_WINDOW["face_gap"]) * kx), _even(face_y * ky), _even(fs * kx))
    if shot.card_box:
        bx, by, bw = shot.card_box
        cw = _even(bw * w)
        card = (_even(bx * w), _even(by * h), cw, _even(cw / aspect))
    if shot.face_box:
        fx, fy, size = shot.face_box
        face = (_even(fx * w), _even(fy * h), _even(size * w))
    return {"card": card, "face": face, "border": border, "radius": radius}


def _rounded_mask(cw: int, ch: int, r: int, fps: Fraction, frames: int) -> str:
    """Маска скруглённого прямоугольника: один кадр через geq, дальше повтор."""
    inside = (f"lte(pow(max(max({r}-X,X-{cw - 1 - r}),0),2)+"
              f"pow(max(max({r}-Y,Y-{ch - 1 - r}),0),2),{r * r})")
    return (f"color=c=black:s={cw}x{ch}:r={fps.numerator}/{fps.denominator},trim=end_frame=1,format=gray,"
            f"geq=lum='255*{inside}',loop=loop={frames}:size=1:start=0,setpts=N")


def _background(w: int, h: int, fps: Fraction, frames: int) -> str:
    """Тёмный фон с размытой сеткой: один кадр, дальше повтор (размытие не на каждом кадре)."""
    cell = _even(w / 24)
    return (f"color=c={SIDE_BG}:s={w}x{h}:r={fps.numerator}/{fps.denominator},trim=end_frame=1,"
            f"drawgrid=w={cell}:h={cell}:t={max(2, w // 640)}:c={SIDE_GRID},gblur=sigma={max(4, w // 160)},"
            f"loop=loop={frames}:size=1:start=0,setpts=N")


def _drop_xy(sh: Shot, x: int, y: int, h: int, fps: Fraction) -> str:
    """Место рамки экрана: неподвижно или с опусканием (drop) — плавно вниз
    в начале участка, вверх в конце."""
    if not sh.drop:
        return f"{x}:{y}"
    frames = sh.end - sh.start
    k = max(1, min(round(DROP_RAMP_S * fps), frames // 2))
    q = f"min(1,min(n/{k},({frames - 1}-n)/{k}))"
    return f"x={x}:y='{y}+{round(sh.drop * h)}*{q}*{q}*(3-2*{q})':eval=frame"


def _side(i: int, sh: Shot, w: int, h: int, fps: Fraction, scr_size: tuple[int, int], cut: str,
          bg: str | None = None) -> list[str]:
    g = side_geometry(sh, w, h, *scr_size)
    x, y, cw, ch = g["card"]
    fx, fy, fs = g["face"]
    b = g["border"]
    frames = sh.end - sh.start
    rx, ry, rw, rh = sh.region or (0.0, 0.0, 1.0, 1.0)
    crop = "" if sh.region is None else f"crop=iw*{rw:.6f}:ih*{rh:.6f}:iw*{rx:.6f}:ih*{ry:.6f},"
    inner = fs - 2 * b
    return [
        (f"{bg}fps={fps.numerator}/{fps.denominator},trim=end_frame={frames},setpts=PTS-STARTPTS,"
         f"{_fill(w, h)},format=yuv420p[bg{i}]") if bg else f"{_background(w, h, fps, frames)}[bg{i}]",
        f"{_rounded_mask(cw, ch, g['radius'], fps, frames)}[m{i}]",
        f"[s{i}]{cut},{crop}scale={cw}:{ch},setsar=1,format=yuva420p[sc{i}]",
        f"[sc{i}][m{i}]alphamerge[card{i}]",
        f"[c{i}]{cut},crop=ih:ih:max(0\\,min(iw-ih\\,iw*{sh.face_cx:.6f}-ih/2)):0,"
        f"scale={inner}:{inner},setsar=1,pad={fs}:{fs}:{b}:{b}:color=white[f{i}]",
        f"[bg{i}][card{i}]overlay={_drop_xy(sh, x, y, h, fps)}:shortest=1[bc{i}]",
        f"[bc{i}][f{i}]overlay={fx}:{fy}:shortest=1,format=yuv420p[o{i}]",
    ]


def _whip(i: int, w: int, h: int, fps: Fraction, frames: int, head: bool, tail: bool) -> str:
    """Шторка для участка i ([o{i}pre] -> [o{i}]): в начале кадр въезжает снизу, в
    конце уезжает вверх; пока едет — размыт по вертикали."""
    k = max(2, min(round(WHIP_S * fps), frames // 2))
    start = frames - k
    y_tail = f"if(gte(n,{start}),-{h}*pow((n-{start})/{k},2),0)" if tail else "0"
    y = f"if(lt(n,{k}),{h}*pow(1-n/{k},2),{y_tail})" if head else y_tail
    on = "+".join(([f"lt(n,{k})"] if head else []) + ([f"gte(n,{start})"] if tail else []))
    blur = max(8, h // 20)
    return (f"color=c=0x121212:s={w}x{h}:r={fps.numerator}/{fps.denominator},trim=end_frame={frames},"
            f"setpts=N[wb{i}];"
            f"[o{i}pre]gblur=sigma=0.01:sigmaV={blur}:enable='{on}'[wm{i}];"
            f"[wb{i}][wm{i}]overlay=x=0:y='{y}':eval=frame:shortest=1,format=yuv420p[o{i}]")


def compose_graph(plan: VisualPlan, screen: str, camera: str | None, fps: Fraction, cam_size: tuple[int, int],
                  scr_size: tuple[int, int] | None = None, clips: dict[int, str] | None = None) -> str:
    """Участки раскладки из потоков [screen] и [camera] -> [v]. clips — номер
    участка broll -> поток его клипа (открыт с нужной секунды), номер участка
    side с видео-фоном -> поток фона."""
    w, h = plan.width, plan.height
    shots = plan.shots
    parts: list[str] = []
    cam_shots = [i for i, s in enumerate(shots) if s.uses_camera]
    if len(shots) == 1:
        parts.append(f"{screen}null[s0]")
    else:
        parts.append(f"{screen}split={len(shots)}" + "".join(f"[s{i}]" for i in range(len(shots))))
    if cam_shots:
        if camera is None:
            raise ValueError("в плане есть лицо, а потока камеры нет")
        if len(cam_shots) == 1:
            parts.append(f"{camera}null[c{cam_shots[0]}]")
        else:
            parts.append(f"{camera}split={len(cam_shots)}" + "".join(f"[c{i}]" for i in cam_shots))
    for i, sh in enumerate(shots):
        cut = f"trim=start_frame={sh.start}:end_frame={sh.end},setpts=PTS-STARTPTS"
        if sh.layout == "screen":
            look = _zoom(sh, w, h, fps) if sh.zoom else _fit(w, h)
            parts.append(f"[s{i}]{cut},{look},format=yuv420p[o{i}]")
        elif sh.layout == "camera":
            # Экран этого участка не показывается, но его ветку надо прочитать
            parts.append(f"[s{i}]{cut},nullsink")
            motion = _camera_motion(sh, fps)
            if motion:
                look = f"{motion},{_fill(w, h)}"  # движение на кадре камеры, потом в размер ролика
            else:
                look = _punch(sh, w, h) if sh.punch else _fill(w, h)
            parts.append(f"[c{i}]{cut},{look},format=yuv420p[o{i}]")
        elif sh.layout == "side":
            parts += _side(i, sh, w, h, fps, scr_size or (w, h), cut, (clips or {}).get(i))
        elif sh.layout == "broll":
            if not clips or i not in clips:
                raise ValueError("в плане свой кадр, а потока клипа нет")
            n = sh.end - sh.start
            parts.append(f"[s{i}]{cut},nullsink")
            # Клип в частоту ролика; короче участка — держим последний кадр
            parts.append(f"{clips[i]}fps={fps.numerator}/{fps.denominator},tpad=stop_mode=clone:stop={n},"
                         f"trim=end_frame={n},setpts=PTS-STARTPTS,{_fill(w, h)},format=yuv420p[o{i}]")
        else:  # pip
            x, y, pw, ph = pip_geometry(sh, w, h, *cam_size)
            parts.append(f"[s{i}]{cut},{_fit(w, h)}[b{i}]")
            look = _punch(sh, pw, ph) if sh.punch else _fill(pw, ph)
            parts.append(f"[c{i}]{cut},{look}[p{i}]")
            parts.append(f"[b{i}][p{i}]overlay={x}:{y}:shortest=1,format=yuv420p[o{i}]")
    # Шторки: участок с enter=whip въезжает снизу, предыдущий уезжает вверх
    for i, sh in enumerate(shots):
        head = sh.whip_in if sh.whip_in is not None else (sh.enter == "whip" and i > 0)
        tail = sh.whip_out if sh.whip_out is not None else (i + 1 < len(shots) and shots[i + 1].enter == "whip")
        if head or tail:
            parts = [p.replace(f"[o{i}]", f"[o{i}pre]") for p in parts]
            parts.append(_whip(i, w, h, fps, sh.end - sh.start, head, tail))
    joined = "".join(f"[o{i}]" for i in range(len(shots)))
    tail = f"{joined}concat=n={len(shots)}:v=1:a=0," if len(shots) > 1 else f"{joined}null,"
    parts.append(f"{tail}settb={fps.denominator}/{fps.numerator},setpts=N[v]")
    return ";".join(parts)
