"""
Навигатор (D-8, ALG-3): код ведёт товар по дереву ТН ВЭД, модель только выбирает ветку.

Дерево — ступени тарифа из full_path действующих кодов («насосы центробежные прочие →
насосы погружные → одноступенчатые»), а не пары цифр: развилки тарифа идут по тире и
часто режут одну 8-значную подсубпозицию. Корень — товарная позиция (4 знака).

На каждом узле с несколькими ветками канал решений (decisions.choose) выбирает ветку
и даёт вероятности. Уверенность не ниже порога — спускаемся сами; ниже — останавливаемся
и отдаём вопрос: ветки узла с вероятностями, человек выбирает кнопкой. Ответ — путь
(подписи веток по порядку), с него спуск продолжается. Код, на котором остановились,
честный: 4, 6 или 8 знаков, если дальше без человека не пройти.

Узел, где все ветки делятся по одной величине («мощностью не более 30 кВт» / «более 30 кВт»),
а в описании ровно одно её значение, проходит правило (conditions.choose) без модели: шаг
с полем rule, уверенность 1. Самопроверка по дереву 30.10 — 735 срабатываний,
0 не в ту ветку; на путях решений ЕЭК — 1 срабатывание из 13 узлов с условиями, верное.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import conditions
from tnved_data import DB_PATH, TNVEDStore, is_current_leaf

SEP = "→"
# Замер 30.10 (ЕЭК, 88 случаев, старт с кандидатов): при 0,8 без человека 19 верно и 5 ошибок,
# с человеком 81/88; при 0,85 — 13 верно и 1 ошибка, с человеком 85/88, вопросов 1,45 на товар.
# Для открытой страницы уверенная ошибка дороже лишнего вопроса. Jev отдаёт уверенность с двумя
# знаками, и ровно 0,85 в повторе ЕЭК и на 159 обычных товарах ФСА — 7 решений, 3 неверных
# (8708 вместо 8302, 9505 вместо 9503, 9004 вместо 6506): с 0,86 на них вопрос.
NAV_THRESHOLD = float(os.environ.get("NAV_THRESHOLD", "0.86"))
_OTHER = re.compile(r"^\s*проч", re.I)
# Подпись ветки «прочие» сама по себе пуста: модель видит, от чего она «прочая».
OTHER_HINT = "прочие — не подходящие ни под один другой вариант этого уровня"
LABEL_LIMIT = 300
# Позиции редакции 2022 без своей строки в справочнике (данные TWS, DATA-1): название — как
# в full_path их кодов. У части их кодов ступени позиции в full_path нет вовсе — у таких дерево
# берёт путь со второй ступени, иначе пропала бы первая развилка (у 8806 — «вертолеты»).
MISSING_HEADINGS = {
    "0309": "Мука тонкого и грубого помола и гранулы из рыбы, ракообразных, моллюсков и прочих водных "
            "беспозвоночных, пригодные для употребления в пищу",
    "2404": "Продукция, содержащая табак, восстановленный табак, никотин, или заменители табака или "
            "никотина, предназначенная для вдыхания без горения; прочая продукция, содержащая никотин "
            "и предназначенная для поступления никотина в организм человека",
    "3827": "Смеси, содержащие галогенированные производные метана, этана или пропана, в другом месте "
            "не поименованные или не включенные",
    "8485": "Машины для аддитивного производства",
    "8524": "Модули с плоской дисплейной панелью, в том числе с сенсорным экраном",
    "8549": "Отходы и лом электротехнических и электронных изделий",
    "8806": "Беспилотные летательные аппараты",
    "8807": "Части летательных аппаратов товарной позиции 8801, 8802 или 8806",
    "9620": "Моноопоры, двуноги, треноги и аналогичные изделия",
}
# Разделы ТН ВЭД: (номер, название, первая группа, последняя). Спуск от корня идёт раздел →
# группа → позиция: 21 вариант вместо 96 групп разом. Модель видит у раздела и его группы.
SECTIONS = [
    ("I", "Живые животные; продукты животного происхождения", 1, 5),
    ("II", "Продукты растительного происхождения", 6, 14),
    ("III", "Жиры и масла животного, растительного или микробиологического происхождения; "
            "готовые пищевые жиры; воски", 15, 15),
    ("IV", "Готовые пищевые продукты; напитки и уксус; табак, никотинсодержащая продукция", 16, 24),
    ("V", "Минеральные продукты", 25, 27),
    ("VI", "Продукция химической и связанных с ней отраслей промышленности", 28, 38),
    ("VII", "Пластмассы и изделия из них; каучук, резина и изделия из них", 39, 40),
    ("VIII", "Шкуры, кожа, натуральный мех и изделия из них; дорожные принадлежности, сумки", 41, 43),
    ("IX", "Древесина и изделия из нее; пробка; плетеные изделия", 44, 46),
    ("X", "Масса из древесины; макулатура; бумага, картон и изделия из них", 47, 49),
    ("XI", "Текстильные материалы и текстильные изделия", 50, 63),
    ("XII", "Обувь, головные уборы, зонты, трости; перья; искусственные цветы; изделия из волоса", 64, 67),
    ("XIII", "Изделия из камня, гипса, цемента, асбеста, слюды; керамика; стекло и изделия из него", 68, 70),
    ("XIV", "Жемчуг, драгоценные камни, драгоценные металлы и изделия из них; бижутерия; монеты", 71, 71),
    ("XV", "Недрагоценные металлы и изделия из них", 72, 83),
    ("XVI", "Машины, оборудование и механизмы; электротехническое оборудование; аппаратура записи "
            "и воспроизведения звука и изображения; их части", 84, 85),
    ("XVII", "Средства наземного транспорта, летательные аппараты, плавучие средства", 86, 89),
    ("XVIII", "Инструменты и аппараты оптические, фото, измерительные, медицинские; часы; "
              "музыкальные инструменты", 90, 92),
    ("XIX", "Оружие и боеприпасы", 93, 93),
    ("XX", "Разные промышленные товары", 94, 96),
    ("XXI", "Произведения искусства, предметы коллекционирования и антиквариат", 97, 97),
]
HINT_LIMIT = 700


def _section(group: str) -> tuple[str, str] | None:
    g = int(group)
    return next(((n, t) for n, t, a, b in SECTIONS if a <= g <= b), None)


@dataclass
class Node:
    label: str
    codes: list[str] = field(default_factory=list)
    children: dict[str, "Node"] = field(default_factory=dict)
    own: list[str] = field(default_factory=list)
    hint: str = ""  # текст для модели вместо подписи: у раздела — его группы

    @property
    def prefix(self) -> str:
        """Общее начало кодов узла, до чётной длины: столько знаков известно."""
        p = os.path.commonprefix(self.codes)
        return p[: len(p) // 2 * 2]

    def options(self) -> list["Node"]:
        """Ветки узла. Код, путь которого кончается на узле с ветками («прочие» без
        своего «прочие» ниже), становится отдельной веткой «прочие»."""
        opts = list(self.children.values())
        if opts and self.own:
            label = "прочие" if "прочие" not in self.children else "прочие (остальные)"
            opts.append(Node(label=label, codes=list(self.own), own=list(self.own)))
        return opts


class Tree:
    """Корень → разделы → группы → позиции → ступени тарифа; только действующие 10-значные
    коды. Путь к узлу — подписи от корня: [раздел, группа, позиция, ступень, …]."""

    def __init__(self, store: TNVEDStore):
        with sqlite3.connect(DB_PATH) as conn:
            names = dict(conn.execute("SELECT code, description FROM codes WHERE length(code) IN (2, 4)"))
        self.top = Node(label="ТН ВЭД ЕАЭС")
        self.roots: dict[str, Node] = {}           # позиция → её узел
        self.heading_path: dict[str, list[str]] = {}
        for item in sorted(store.meta, key=lambda m: m["code"]):
            if not is_current_leaf(item):
                continue
            code = item["code"]
            h = code[:4]
            if h not in self.roots:
                glabel = f"{h[:2]} {(names.get(h[:2]) or '').strip()}".strip()
                hlabel = f"{h} {(names.get(h) or MISSING_HEADINGS.get(h, '')).strip()}".strip()
                sec = _section(h[:2])
                up = self.top
                if sec:
                    slabel = f"Раздел {sec[0]}. {sec[1]}"
                    up = self.top.children.setdefault(slabel, Node(label=slabel))
                group = up.children.setdefault(glabel, Node(label=glabel))
                self.roots[h] = group.children[hlabel] = Node(label=hlabel)
                self.heading_path[h] = ([slabel] if sec else []) + [glabel, hlabel]
            self.top.codes.append(code)
            for i in range(1, len(self.heading_path[h])):
                self.walk(self.heading_path[h][:i]).codes.append(code)
            node = self.roots[h]
            node.codes.append(code)
            levels = _levels(item)
            own_step = h in names or (len(levels) > 1 and _same(levels[1], MISSING_HEADINGS.get(h, "")))
            for seg in levels[2 if own_step else 1:]:
                node = node.children.setdefault(seg, Node(label=seg))
                node.codes.append(code)
            node.own.append(code)
        for sec in self.top.children.values():
            if sec.label.startswith("Раздел "):
                groups = "; ".join(g.label[:90] for g in sec.children.values())
                sec.hint = f"{sec.label}. Группы: {groups}"[:HINT_LIMIT]

    def walk(self, path: list[str]) -> Node:
        """Узел по пути из подписей от корня; KeyError — пути в дереве нет."""
        node = self.top
        for label in path:
            found = next((o for o in node.options() if o.label == label), None)
            if found is None:
                raise KeyError(label)
            node = found
        return node


def _levels(item: dict) -> list[str]:
    return [s.strip() for s in (item.get("full_path") or "").split(SEP) if s.strip()]


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().replace("ё", "е").split()) == " ".join(b.lower().replace("ё", "е").split())


Decide = Callable[[str, str, dict[str, str]], Awaitable["object"]]

CANDIDATES_K = int(os.environ.get("NAV_CANDIDATES", "8"))
# Примечание 2 к разделу XV, 1 к разделу XVI, 2 к разделу XVII: «части общего назначения»
# (крепёж 7318, пружины 7320, арматура и фурнитура 8302) в «части» машин и транспорта не входят.
# Поиск их часто не находит: «салазки крепления сиденья» тянут в 8708, ЕЭК решила 8302 30.
# Есть среди кандидатов позиция «частей» — к ней в пару эти три.
PARTS_HEADINGS = {"8409", "8431", "8448", "8466", "8473", "8503", "8529", "8538",
                  "8607", "8708", "8714", "8716", "8803"}
GENERAL_USE = ["7318", "7320", "8302"]
ELSEWHERE = "другая позиция — товар не подходит ни под одну из перечисленных"


def candidate_headings(store: TNVEDStore, precedents, description: str, valid: set[str],
                       k: int = CANDIDATES_K) -> list[str]:
    """Позиции-кандидаты: первые k от прецедентов ФСА и первые k от векторного поиска.
    Замер 29.10 (ЕЭК, 88 случаев): верная позиция среди них в 94% при k=8 (≈13 позиций),
    90% при k=5; поодиночке — 74% и 83%."""
    pre = [h for h, _ in precedents.headings(description, valid, limit=k)] if precedents else []
    vec: list[str] = []
    for item in store.search(description, top_k=8 * k):
        h = item["code"][:4]
        if h in valid and h not in vec:
            vec.append(h)
            if len(vec) >= k:
                break
    out = list(dict.fromkeys(pre + vec))
    if PARTS_HEADINGS & set(out):
        out += [h for h in GENERAL_USE if h in valid and h not in out]
    return out


def _criteria(options: list[Node]) -> dict[str, str]:
    crit: dict[str, str] = {}
    for o in options:
        key = o.label[:120]
        while key in crit:
            key += "·"
        crit[key] = (o.hint or (OTHER_HINT if _OTHER.match(o.label) and len(o.label) < 12
                                else o.label[:LABEL_LIMIT]))
    return crit


def _instructions(trail: list[str]) -> str:
    if not trail:
        return "Товар классифицируется по ТН ВЭД ЕАЭС. В какую группу ТН ВЭД он входит?"
    return ("Товар классифицируется по ТН ВЭД ЕАЭС; уже выбрано: "
            + " → ".join(t[:150] for t in trail)
            + ". Какой вариант следующего уровня подходит этому товару?")


async def navigate(tree: Tree, description: str, decide: Decide, path: list[str] | None = None,
                   threshold: float = NAV_THRESHOLD, candidates: list[str] | None = None) -> dict:
    """Спуск от узла по path до листа или до первого неуверенного узла.

    path — подписи от корня ([] — от корня; от позиции — tree.heading_path[h]). Если path
    не задан (None) и есть candidates (позиции, candidate_headings), первый шаг — выбор
    среди них или «другая позиция» (тогда спуск от корня по группам).

    decide(state, instructions, criteria) → объект с choice, confidence, probabilities
    (decisions.Choice). Возвращает: путь, шаги с вероятностями, код (сколько знаков
    известно), коды-кандидаты под узлом и вопрос, если остановились до листа. У каждого
    варианта вопроса и шага — path: с него продолжать спуск, если человек выбрал его."""
    steps: list[dict] = []
    cost = 0.0
    question = None
    trail = list(path or [])
    if path is None and candidates:
        heads = [h for h in candidates if h in tree.roots]
        crit = _criteria([tree.roots[h] for h in heads]) | {"другая позиция": ELSEWHERE}
        target = {k: tree.heading_path[h] for k, h in zip(crit, heads)} | {"другая позиция": []}
        ans = await decide(description, "Товар классифицируется по ТН ВЭД ЕАЭС. "
                           "В какую из этих товарных позиций он входит?", crit)
        cost += getattr(ans, "cost_usd", 0.0)
        ranked = sorted(({"label": k if k == "другая позиция" else tree.roots[heads[i]].label,
                          "code": "" if k == "другая позиция" else heads[i], "path": target[k],
                          "p": round(ans.probabilities.get(k, 0.0), 3)} for i, k in enumerate(crit)),
                        key=lambda o: -o["p"])
        steps.append({"path": None, "options": ranked,
                      "choice": next(o["label"] for o in ranked if o["path"] == target[ans.choice]),
                      "confidence": round(ans.confidence, 3)})
        if ans.confidence < threshold:
            question = {"path": None, "title": "К какой товарной позиции ближе товар?",
                        "text": "Уточните, к какой товарной позиции ближе товар:", "options": ranked}
            return _result(tree, tree.top, [], steps, question, cost)
        trail = list(target[ans.choice])
    node = tree.walk(trail)
    while True:
        options = node.options()
        if not options:
            break
        if len(options) == 1:
            node = options[0]
            trail.append(node.label)
            continue
        rule = conditions.choose([o.label for o in options], description)
        if rule is not None:
            i, reason = rule
            ranked = sorted(({"label": o.label, "code": o.prefix, "path": trail + [o.label],
                              "p": float(j == i)} for j, o in enumerate(options)), key=lambda o: -o["p"])
            steps.append({"path": list(trail), "options": ranked, "choice": options[i].label,
                          "confidence": 1.0, "rule": reason})
            node = options[i]
            trail.append(node.label)
            continue
        crit = _criteria(options)
        by_key = dict(zip(crit, options))
        ans = await decide(description, _instructions(trail), crit)
        cost += getattr(ans, "cost_usd", 0.0)
        ranked = sorted(({"label": by_key[k].label, "code": by_key[k].prefix,
                          "path": trail + [by_key[k].label],
                          "p": round(ans.probabilities.get(k, 0.0), 3)} for k in crit),
                        key=lambda o: -o["p"])
        steps.append({"path": list(trail), "options": ranked,
                      "choice": by_key[ans.choice].label, "confidence": round(ans.confidence, 3)})
        if ans.confidence < threshold:
            question = {"path": list(trail), **_question_text(node, options), "options": _noted(ranked)}
            break
        node = by_key[ans.choice]
        trail.append(node.label)
    return _result(tree, node, trail, steps, question, cost)


def _result(tree: Tree, node: Node, trail: list[str], steps: list[dict], question: dict | None,
            cost: float) -> dict:
    for o in [o for s in steps for o in s["options"]] + (question["options"] if question else []):
        o.setdefault("name", node_name(o["label"]))
        o.setdefault("short", _short(o["name"]))
    ladder, up = [], tree.top
    for label in trail:  # «лестница»: код и официальный текст на каждом уровне
        up = next(o for o in up.options() if o.label == label)
        ladder.append({"code": up.prefix, "name": node_name(label)})
    return {"path": trail, "path_codes": ladder, "code": node.prefix, "digits": len(node.prefix),
            "codes": node.codes[:20], "n_codes": len(node.codes), "steps": steps,
            "question": question, "done": question is None and len(node.codes) == 1,
            "cost_usd": round(cost, 6)}


def node_name(label: str) -> str:
    """Официальный текст узла без кода группы или позиции; label — ключ пути."""
    return re.sub(r"^\d{2}(?:\d{2})?\s+", "", label).strip() or label


def _short(name: str, limit: int = 80) -> str:
    """Для кнопки: до первой «;», не длиннее limit по границе слова."""
    s = name.split(";")[0].strip()
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0].rstrip(",:") + "…"


def _question_text(node: Node, options: list[Node]) -> dict:
    """title — короткий заголовок для страницы, text — с подсказкой и узлом в скобках."""
    if node.label == "ТН ВЭД ЕАЭС":
        return {"title": "К какому разделу ближе товар?", "text": "Уточните, к какому разделу ближе товар:"}
    by = conditions.split_by([o.label for o in options])
    if by:
        return {"title": f"Код зависит от {by}",
                "text": f"Код зависит от {by} — выберите диапазон или допишите значение в описание "
                        f"({node.label[:160]}):"}
    return {"title": "Что из этого подходит товару?",
            "text": f"Уточните, что из этого подходит товару ({node.label[:160]}):"}


def _noted(ranked: list[dict]) -> list[dict]:
    """«Прочие» человеку ничего не говорят: note — от чего они прочие (подписи соседей)."""
    others = [o["label"] for o in ranked if not _OTHER.match(o["label"])]
    for o in ranked:
        if _OTHER.match(o["label"]) and len(o["label"]) < 25 and others:
            o["note"] = "не подходит ни под один вариант: " + "; ".join(node_name(x) for x in others[:4]) \
                        + (f"; и ещё {len(others) - 4}" if len(others) > 4 else "")
    return ranked
