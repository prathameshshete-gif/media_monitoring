"""Turn the article corpus into a written media-monitoring report via Gemini.

Built on LangChain: a ChatPromptTemplate holds the analyst brief, and the chain
is `prompt | llm | StrOutputParser()`. Swapping models means swapping the chat
model object; the prompt and the callers stay as they are.

The report is written in Marathi. Facts that can be counted exactly — how many
articles, which outlets — are counted here in Python and handed to the model,
rather than left for it to tally out of the corpus.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

load_dotenv()

# gemini-2.5-pro is the stable fallback if a preview id is ever withdrawn.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")

# A full Marathi report with tables runs long, and Devanagari costs more tokens
# per word than Latin. 65536 is the model's ceiling.
MAX_OUTPUT_TOKENS = 65536

SYSTEM_PROMPT = """\
तुम्ही महाराष्ट्राच्या राजकीय परिसंस्थेचा आणि पक्षीय गतिशीलतेचा सखोल अभ्यास \
असलेले वरिष्ठ राजकीय व माध्यम विश्लेषक आहात. तुमचा ग्राहक हा एक वरिष्ठ \
संस्थात्मक भागधारक आहे, ज्याला राजकीय रणनीती व भागधारक-संवादासाठी विषयाच्या \
माध्यम-दृश्यमानतेचे, संदेशनाचे आणि प्रतिमेचे अत्यंत परिपक्व, पक्षनिरपेक्ष \
मूल्यमापन हवे आहे.

## इनपुट

तुम्हाला बातम्यांच्या लेखांचा संपूर्ण मजकूर दिला जाईल. प्रत्येक लेख \
`=== ARTICLE n ===` अशा क्रमांकित सीमांकनाने वेगळा केलेला असतो आणि त्यासोबत \
त्याचा स्रोत (Source), प्रसिद्धीची वेळ (Published), भाषा (Language), URL आणि \
सुसंगतता-गुण (Relevance) दिलेला असतो. लेख मराठी, हिंदी व इंग्रजीत आहेत — ते \
सर्व वाचा.

## भाषा आणि लेखनशैली

- संपूर्ण अहवाल **मराठीत** लिहा. सर्व शीर्षके, उपशीर्षके, तक्त्यांचे स्तंभ, \
विश्लेषण व निष्कर्ष मराठीतच असावेत.
- हिंदी व इंग्रजी लेखांतील आशय मराठीत भाषांतरित करून मांडा. अवतरणे मराठीत द्या; \
जिथे मूळ शब्दरचना महत्त्वाची असेल तिथे कंसात मूळ वाक्य द्या.
- पक्ष, संस्था, योजना व पदनामांची रूढ मराठी रूपे वापरा. आवश्यक तिथे इंग्रजी \
संज्ञा कंसात द्या.
- प्रत्येक व्यक्तीचा उल्लेख करताना **पूर्ण नाव** आणि आदरार्थी उपसर्ग वापरा — \
"श्री. आशिष शेलार" असे, "श्री शेलार" असे नाही. "श्री" ऐवजी नेहमी "श्री." \
(पूर्णविरामासह) लिहा. महिलांसाठी "श्रीमती." वापरा.
- संख्या अंकांमध्ये (1, 2, 3) लिहा.
- भाषा स्पष्ट आणि व्यावसायिक ठेवा. भरताड मजकूर, जनसंपर्क-शैलीतील स्तुती किंवा \
अनावश्यक सावधगिरीची वाक्ये टाळा.

## अचूकतेचे नियम

- फक्त पुरवलेल्या लेखांतील माहितीच वापरा. विषयाबाबतची बाह्य पार्श्वभूमी जोडू \
नका आणि लेखांत नसलेल्या घटनांचा अंदाज बांधू नका.
- प्रत्येक विधानानंतर संदर्भ म्हणून लेखक्रमांक चौकोनी कंसात द्या: [3], [7][12].
- लेखांमध्ये परस्परविरोध असल्यास तो स्पष्टपणे नोंदवा आणि दोन्ही संदर्भ द्या.
- एखादा लेख विषयाशी केवळ ओझरता संबंधित असल्यास तसे स्पष्ट म्हणा; त्याचे महत्त्व \
वाढवून सांगू नका.
- वृत्तांकन तुटपुंजे असेल तिथे "उपलब्ध पुरावा मर्यादित आहे" असे स्पष्ट नमूद करा.
- मजकूर अवाचनीय किंवा अनुपलब्ध असल्यास ते स्पष्टपणे सांगा आणि उपलब्ध \
माहितीच्या आधारे पुढे जा.

## रचना (Markdown)

अहवाल GitHub-flavoured Markdown मध्ये द्या. संपूर्ण अहवाल कोड-फेन्समध्ये \
गुंडाळू नका. मुख्य विभागांसाठी `##`, उपविभागांसाठी `###` वापरा. तक्ते \
पाइप-सिंटॅक्समध्ये शीर्षक-ओळ व विभाजक-ओळीसह द्या. मुद्द्यांच्या यादीत \
सुरुवातीचा ठळक शब्द `**असा**` ठेवा.

खालील विभाग याच क्रमाने लिहा:

## परिचय

गेल्या कालावधीतील विषयाशी संबंधित प्रमुख घडामोडींचा थोडक्यात आढावा. युती-राजकारण, \
प्रशासकीय भूमिका, जाहीर भाषणे, वादांची हाताळणी, विधिमंडळ कामकाज यांसारखे \
पुनरावृत्त राजकीय प्रवाह अधोरेखित करा. या आठवड्याच्या वृत्तांकनाचे धोरणात्मक \
महत्त्व सांगा आणि **अहवाल किती लेखांवर आधारित आहे त्याची संख्या येथे नमूद करा**.

## प्रमुख घडामोडी

### ठळक घडामोडी

विषयाशी संबंधित सर्वाधिक चर्चिल्या गेलेल्या किंवा पुनरावृत्त विषयांची यादी. \
प्रत्येक विषयासाठी काय घडले, ते कोणत्या वृत्तसंस्थांनी दिले, त्यांची मांडणी कशी \
होती, आणि त्याचे राजकीय, प्रशासकीय व निवडणूकविषयक महत्त्व काय — हे विस्ताराने \
लिहा. मराठी व हिंदी वृत्तांकनातील भर किंवा सुरातील फरक नोंदवा.

### मर्यादित प्रसिद्धीतील घडामोडी

महत्त्वाच्या पण कमी वृत्तांकन मिळालेल्या घडामोडी. प्रत्येकीसाठी ती महाराष्ट्राच्या \
राजकीय पटलावर किंवा धोरणात्मक वातावरणात का महत्त्वाची आहे ते थोडक्यात स्पष्ट \
करा. विषयाच्या दृष्टिकोनातून **सकारात्मक** व **नकारात्मक** अशी स्वतंत्र \
विभागणी करा.

## लेखांमधील प्रमुख नेते

लेखांमध्ये विषयासोबत उल्लेख आलेल्या राजकीय व प्रशासकीय व्यक्तींचा क्रमवारीत \
तक्ता. स्तंभ: अनुक्रमांक | नाव | मुख्य विधाने/कृती | उल्लेख संख्या. \
उल्लेख संख्या म्हणजे ती व्यक्ती ज्या *वेगवेगळ्या* लेखांत येते त्यांची संख्या — \
ती काळजीपूर्वक व अचूक मोजा, आणि तक्ता उतरत्या क्रमाने लावा. विषय स्वतःही \
तक्त्यात पहिल्या ओळीत असावा. हा विभाग काटेकोरपणे तथ्याधारित व पक्षनिरपेक्ष ठेवा.

तक्त्याखाली ही ओळ जशीच्या तशी लिहा:
टीप: उल्लेख संख्या अंदाजित, उपलब्ध सारांशावर आधारित.

## प्रसिद्धीची कारणे

या कालावधीत विषय माध्यमांच्या केंद्रस्थानी का राहिला याचे विश्लेषण, \
**सकारात्मक घटक** व **नकारात्मक घटक** अशा दोन उपविभागांत. प्रत्येक घटकाने \
दृश्यमानतेत किंवा माध्यम-मांडणीत नेमकी काय भर घातली ते स्पष्ट करा.

## एकूण विश्लेषण आणि निष्कर्ष

या कालावधीतील माध्यम-प्रतिमेचा सुसंगत विश्लेषणात्मक आढावा: वृत्तांकनाचा सूर \
(तटस्थ, टीकात्मक, सकारात्मक, ध्रुवीकृत), माध्यमांचे लक्ष वेधणारे मुद्दे, आणि \
नेतृत्वाची प्रतिमा किती प्रभावीपणे प्रक्षेपित झाली. पुनरावृत्त धोरणात्मक सूत्रे, \
उदयोन्मुख कथानके व विरोधकांची व्यूहरचना यांसारखे नमुने ओळखा. शेवटी 3-4 \
**कृतीयोग्य शिफारशी** द्या — माध्यम-संवाद रणनीती, जोखीम-व्यवस्थापन, प्रतिमा-संवर्धन \
आणि राजकीय स्थाननिश्चिती. निष्कर्ष महाराष्ट्राच्या व्यापक राजकारणाशी जोडा.

## स्रोत

50-100 शब्दांत: अहवाल एकूण किती लेखांवर आधारित आहे ती संख्या, आणि ज्या \
वृत्तसंस्थांचे लेख समाविष्ट आहेत त्यांची यादी मुद्द्यांच्या स्वरूपात. URL देऊ नका.

## अतिरिक्त विभाग

वरील रचनेनंतर, उपलब्ध लेखांतून सिद्ध होणारे आणखी राजकीयदृष्ट्या उपयुक्त विभाग \
जोडा — उदाहरणार्थ प्रादेशिक वृत्तांकनाचे वितरण, विषयनिहाय वारंवारता, विरोधकांचा \
प्रतिसाद, किंवा जोखीम-निर्देशक. प्रत्येक अतिरिक्त विभाग लेखांतील पुराव्यावरच \
आधारित असावा.

## सोशल मीडिया

जर पुरवलेल्या मजकुरात फेसबुक पोस्ट किंवा सोशल मीडिया एंगेजमेंटची आकडेवारी \
प्रत्यक्ष उपलब्ध असेल, तरच फेसबुक विश्लेषण व एंगेजमेंट विश्लेषणाचा विभाग \
मुद्द्यांच्या स्वरूपात लिहा — पोस्टचे प्रकार, त्यांची संख्या व टक्केवारी, आणि \
लोकांवरील परिणाम. **अशी आकडेवारी उपलब्ध नसेल, तर हा विभाग वगळा आणि फक्त एका \
ओळीत नमूद करा की या अहवालात सोशल मीडिया डेटा समाविष्ट नाही.** फेसबुक आकडेवारीचा \
अंदाज बांधू नका किंवा ती काल्पनिकरीत्या तयार करू नका."""

HUMAN_PROMPT = """\
विषय (Subject): {subject}
कालावधी (Coverage window): {window}
एकूण लेख (Total articles): {n_articles}
वृत्तसंस्था (Outlets, with article counts): {outlets}

खालील लेखांच्या आधारे अहवाल लिहा.

{corpus}"""

def build_prompt(system_prompt: str) -> ChatPromptTemplate:
    """Build the chain's prompt from the brief it is given, and only that.

    There is no fallback or resolution here: whoever calls this decides which
    brief an entity gets. Each profile stores its own, edited in the UI's
    settings dialog, and SYSTEM_PROMPT is only the starting text a profile is
    seeded with.

    The brief is template *text*, not a value, so any brace a user types would
    be read as a `{variable}` and blow up at format time. Doubling them makes
    them literal. Article text is passed as a value and needs no such care.
    """
    if not (system_prompt or "").strip():
        raise ValueError("No system prompt for this entity — set one in Settings")
    safe = system_prompt.replace("{", "{{").replace("}", "}}")
    return ChatPromptTemplate.from_messages([
        ("system", safe),
        ("human", HUMAN_PROMPT),
    ])


# The UI sends dates as MM/DD/YYYY. Handed to the model raw, "08/01/2026" reads
# as 8 January under the day-first convention every Indian outlet uses, and the
# report opens with the wrong month. Spell the range out instead.
_MR_MONTHS = ("जानेवारी", "फेब्रुवारी", "मार्च", "एप्रिल", "मे", "जून", "जुलै",
              "ऑगस्ट", "सप्टेंबर", "ऑक्टोबर", "नोव्हेंबर", "डिसेंबर")


def format_window(start: str, end: str) -> str:
    """Turn two MM/DD/YYYY dates into an unambiguous Marathi date range."""
    try:
        a = datetime.strptime(start, "%m/%d/%Y")
        b = datetime.strptime(end, "%m/%d/%Y")
    except (ValueError, TypeError):
        return f"{start} – {end}"      # unrecognised: pass through untouched
    spell = lambda d: f"{d.day} {_MR_MONTHS[d.month - 1]} {d.year}"
    # The ISO pair removes any remaining doubt about day-first vs month-first.
    return (f"{spell(a)} ते {spell(b)} "
            f"({a:%Y-%m-%d} – {b:%Y-%m-%d})")


def corpus_facts(corpus_text: str) -> tuple[int, str]:
    """Count articles and outlets from the corpus.

    The prompt asks the report to state how many articles it rests on and which
    outlets they came from. Counting here makes those two numbers exact instead
    of leaving the model to tally hundreds of delimiters by eye.
    """
    n = len(re.findall(r"^=== ARTICLE \d+ ===", corpus_text, re.M))
    outlets = Counter(re.findall(r"^Source:\s*(.+?)\s*$", corpus_text, re.M))
    listed = ", ".join(f"{name} ({count})"
                       for name, count in outlets.most_common())
    return n, listed


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "No Gemini credentials. Put GEMINI_API_KEY=... in .env at the "
            "project root (see README)."
        )
    return key


def build_llm(model: str = MODEL, temperature: float = 0.2,
              max_output_tokens: int = MAX_OUTPUT_TOKENS) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=_api_key(),
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def build_report(corpus_text: str, subject: str, window: str,
                 model: str = MODEL, temperature: float = 0.2,
                 max_output_tokens: int = MAX_OUTPUT_TOKENS, llm=None,
                 system_prompt: str = SYSTEM_PROMPT,
                 verbose: bool = True) -> str:
    """Send the corpus through the chain and return the written report.

    `system_prompt` is the brief this run uses. Callers with a profile pass its
    stored brief; the default here is only for the CLI entry point.
    """
    llm = llm or build_llm(model, temperature, max_output_tokens)
    prompt = build_prompt(system_prompt)
    chain = prompt | llm | StrOutputParser()
    n_articles, outlets = corpus_facts(corpus_text)
    inputs = {"subject": subject, "window": window, "corpus": corpus_text,
              "n_articles": n_articles, "outlets": outlets}

    if verbose:
        try:
            n = llm.get_num_tokens(prompt.format(**inputs))
            print(f"Input: {n:,} tokens to {model} ({n_articles} articles)")
        except Exception as e:            # counting is a nicety, not the job
            print(f"(token count unavailable: {e})")

    # Streamed: a long corpus plus a long report can exceed the non-streaming
    # HTTP timeout.
    parts = [chunk for chunk in chain.stream(inputs)]
    report = "".join(parts).strip()

    # The model occasionally wraps the whole report in a markdown fence despite
    # being told not to; unwrap rather than fail.
    if report.startswith("```"):
        report = re.sub(r"^```[a-zA-Z]*\n", "", report)
        report = re.sub(r"\n```$", "", report).strip()

    if not report:
        raise RuntimeError(
            f"{model} returned an empty report — usually a safety block or a "
            "hit output-token cap."
        )
    if verbose:
        print(f"Output: {len(report):,} chars")
    return report


def run(corpus_path="corpus.txt", subject="Devendra Fadnavis",
        window="30-31 August 2026", out="report_llm.md", **kw) -> str:
    text = Path(corpus_path).read_text(encoding="utf-8")
    report = build_report(text, subject, window, **kw)
    Path(out).write_text(report, encoding="utf-8")
    print(f"Wrote {out} ({len(report):,} chars)")
    return report


if __name__ == "__main__":
    run()
