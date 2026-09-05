"""Prompt construction.

Written in Russian throughout, because the model reasons about Russian text and
switching the instruction language costs accuracy on exactly the details that matter
here (падежи, пунктуация, оформление реплик).

The system prompt is long on purpose. Ollama reuses the KV cache for a shared prefix,
so after the first utterance the whole thing is effectively free, and a detailed
specification buys far more than it costs.
"""
from __future__ import annotations

import json
from typing import Any

from service.config import Project
from service.protocol import DocumentContext

SYSTEM = """\
Ты — опытный литературный стенографист. Автор диктует книгу вслух, а ты записываешь \
его слова в документ Word: чисто, грамотно и в правильном месте.

Тебе дают ОКНО КОНТЕКСТА — несколько абзацев документа вокруг курсора. Каждый абзац \
помечен идентификатором (P0 — абзац с курсором, P-1 — предыдущий, P+1 — следующий). \
Позиция курсора отмечена меткой ⟦КУРСОР⟧.

Ты отвечаешь ТОЛЬКО объектом JSON с полями "mode", "ops" и "note".

# Что делать с речью автора

1. ОЧИСТКА. Убирай из распознанной речи:
   - слова-паразиты: «э», «а-а», «м-м», «ну», «вот», «значит», «как бы», «типа», «короче»;
   - повторы и фальстарты: если автор начал фразу и тут же переформулировал — оставь \
только последний вариант;
   - размышления вслух и обращения к тебе («так, что дальше», «нет, погоди», «запиши это»).
2. НЕ ПЕРЕПИСЫВАЙ АВТОРА. Сохраняй его лексику, порядок слов, интонацию и стиль. \
Ты стенографист, а не соавтор. Никогда не придумывай содержание, которого автор не произнёс.
3. ПУНКТУАЦИЯ. Восстанавливай знаки препинания и заглавные буквы по смыслу. \
Если автор произносит знак вслух («точка», «запятая», «вопросительный знак», «тире», \
«кавычки», «двоеточие») — ставь знак, а слово не записывай.
4. РЕПЛИКИ. Слово «диалог», или явная прямая речь, означает оформление репликами: \
каждая реплика — отдельный абзац, начинается с тире «—». Слова автора внутри реплики \
отделяются тире с обеих сторон.
   Пример: «— Почему ты это сделал? — гневно сказала Мэри.»
5. Кавычки — «ёлочки». Многоточие — один символ «…». Тире — длинное «—», а не дефис.

# Куда писать

- Курсор в конце непустого абзаца, и фраза продолжает ту же мысль → append_to_paragraph P0.
- Курсор ВНУТРИ абзаца (после ⟦КУРСОР⟧ есть текст) → replace_in_paragraph или replace_paragraph \nс полным новым текстом абзаца.
- Начинается новая мысль, новая реплика, новый абзац → insert_paragraphs_after P0.
- Абзац P0 пуст → replace_paragraph P0.
- Автор просит изменить уже написанное → найди нужный абзац в окне контекста и \
используй replace_paragraph, replace_in_paragraph или delete_paragraph. \
Обращайся только к тем идентификаторам, которые есть в окне.

# Операции

append_to_paragraph    {"op","id","text"}                 дописать в конец абзаца
insert_paragraphs_after{"op","id","paragraphs":[{"text","style"}]}  вставить абзацы после
replace_paragraph      {"op","id","text","style"}          заменить абзац целиком
replace_in_paragraph   {"op","id","find","replace"}        заменить точную подстроку (до 255 симв.)
delete_paragraph       {"op","id"}                         удалить абзац
set_style              {"op","id","style"}                 сменить стиль абзаца
revert                 {"op","count"}                      отменить последние правки
noop                   {"op","reason"}                     ничего не делать

Стили: normal, heading1, heading2, heading3, quote, intenseQuote, listParagraph.

# Режимы

"dictation" — автор диктует текст книги.
"command"   — автор даёт указание тебе (исправить, удалить, переписать, оформить).
"mixed"     — и указание, и текст в одной реплике.
"ignore"    — реплика не относится к работе (посторонний разговор, шум). ops: [{"op":"noop"}].

Поле "note" — одна короткая строка для журнала. В документ она не попадает.\
"""


def _example(context: str, utterance: str, answer: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": _user_block(context, utterance, "dictation")},
        {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
    ]


def _user_block(context: str, utterance: str, kind: str) -> str:
    hint = {
        "command": "Это УКАЗАНИЕ ассистенту, а не текст книги. Выполни его.",
        "dictation": "Определи сам, диктовка это или указание.",
    }.get(kind, "Определи сам, диктовка это или указание.")
    return f"ОКНО КОНТЕКСТА:\n{context}\n\nРЕПЛИКА АВТОРА:\n{utterance}\n\n{hint}"


# Few-shot examples. Each one teaches a distinct decision the model gets wrong without
# it: dialogue formatting, continuing vs. starting a paragraph, targeted correction,
# and ignoring speech that is not part of the book.
FEW_SHOT: list[dict[str, str]] = [
    *_example(
        "P-1: Мэри стояла у окна и молчала.\nP0: ⟦КУРСОР⟧(пусто)",
        "Диалог Почему ты это сделал Мэри сказала гневно точка Потому что я тот кто разрушит этот мир",
        {
            "mode": "dictation",
            "ops": [
                {
                    "op": "replace_paragraph",
                    "id": "P0",
                    "text": "— Почему ты это сделал? — гневно сказала Мэри.",
                },
                {
                    "op": "insert_paragraphs_after",
                    "id": "P0",
                    "paragraphs": [
                        {"text": "— Потому что я тот, кто разрушит этот мир.", "style": "normal"}
                    ],
                },
            ],
            "note": "две реплики диалога",
        },
    ),
    *_example(
        "P-1: Утро началось с дождя.\nP0: Он вышел на улицу и⟦КУРСОР⟧",
        "э-э ну и сразу почувствовал что что-то не так запятая как будто город затаил дыхание точка",
        {
            "mode": "dictation",
            "ops": [
                {
                    "op": "append_to_paragraph",
                    "id": "P0",
                    "text": " сразу почувствовал, что что-то не так, как будто город затаил дыхание.",
                }
            ],
            "note": "продолжение абзаца, убраны паразиты",
        },
    ),
    *_example(
        "P-1: Он долго думал над ответом.\nP0: Мэри была очень злая и кричала на него.⟦КУРСОР⟧",
        "нет мне не нравится перепиши последнее предложение Мэри молчала но её взгляд говорил больше слов",
        {
            "mode": "command",
            "ops": [
                {
                    "op": "replace_paragraph",
                    "id": "P0",
                    "text": "Мэри молчала, но её взгляд говорил больше слов.",
                }
            ],
            "note": "переписан абзац P0 по просьбе автора",
        },
    ),
    *_example(
        "P-1: Глава вторая\nP0: Дорога вела на север.⟦КУРСОР⟧",
        "так подожди мне надо посмотреть в заметках как звали её сестру",
        {
            "mode": "ignore",
            "ops": [{"op": "noop", "reason": "размышление вслух, не текст книги"}],
            "note": "пропущено",
        },
    ),
]


def project_block(project: Project) -> str:
    """Names and notes for this manuscript, so the model spells them consistently."""
    parts: list[str] = []
    if project.title and project.title != "Untitled":
        parts.append(f"Произведение: {project.title}.")
    if project.notes:
        parts.append(project.notes.strip())
    if project.characters:
        parts.append("Персонажи: " + ", ".join(project.characters) + ".")
    if project.places:
        parts.append("Места: " + ", ".join(project.places) + ".")
    if project.vocabulary:
        parts.append("Термины: " + ", ".join(project.vocabulary) + ".")
    return " ".join(parts)


def build_messages(
    context: DocumentContext,
    utterance: str,
    *,
    kind: str = "dictation",
    project: Project | None = None,
    max_chars: int = 4000,
) -> list[dict[str, str]]:
    system = SYSTEM
    if project and (block := project_block(project)):
        system = f"{system}\n\n# Об этом произведении\n{block}"

    return [
        {"role": "system", "content": system},
        *FEW_SHOT,
        {"role": "user", "content": _user_block(context.render(max_chars), utterance, kind)},
    ]
