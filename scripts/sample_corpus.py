"""A generated sample library for scripts/benchmark.py: English and Japanese files in folders.

Deterministic (seeded); folders hold from two to a few dozen files. It is built to contain
what makes sorting hard in real libraries:

- folders that are close (two kinds of machine learning; taxes and bank statements);
- habits: files a user keeps in one folder although they read like another (hotel invoices
  under Travel, clinic receipts under Health, rent transfers under Home), which only
  learning from corrections can pick up;
- folders without a description, and folders with only a couple of files;
- meaningless file names (scan_0034.txt) and boilerplate text.
"""

import random
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Habit:
    """Files kept in a folder although they read like another topic."""

    share: float  # of the folder's files
    names: tuple[str, ...]
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class Topic:
    """A folder and what its files say."""

    folder: str
    description: str  # "" for a folder the user never described
    names: tuple[str, ...]
    sentences: tuple[str, ...]
    names_ja: tuple[str, ...] = ()
    sentences_ja: tuple[str, ...] = ()
    habit: Habit | None = None


TOPICS = (
    Topic(
        "Finance/Taxes",
        "tax returns, deductions, withholding slips",
        (
            "tax return {year}",
            "withholding slip {year}",
            "deduction receipts {year}",
            "residence tax notice {year}",
            "hometown tax donation {year}",
        ),
        (
            "The tax return for {year} reports a taxable income of {amount} yen.",
            "Medical expense deductions total {amount2} yen this year.",
            "Withholding tax of {amount2} yen was paid by the employer.",
            "The filing deadline for the {year} return is March 15.",
            "Attach the receipts for the deduction claims to the return.",
            "Residence tax is calculated from last year's income.",
            "The refund of {amount2} yen will be paid to the registered bank account.",
            "Donations under the hometown tax program reduce the residence tax.",
            "The tax office asked for the withholding slip from the second employer.",
            "Consumption tax is not deductible for private expenses.",
        ),
        ("確定申告 {year}", "源泉徴収票 {year}", "住民税決定通知書 {year}"),
        (
            "{year}年分の確定申告書を提出しました。",
            "医療費控除の合計は{amount2}円です。",
            "源泉徴収税額は{amount2}円でした。",
            "住民税は前年の所得から計算されます。",
            "ふるさと納税の寄附金控除を申告します。",
            "還付金は指定の銀行口座に振り込まれます。",
        ),
    ),
    Topic(
        "Finance/Bank",
        "bank statements and transfers",
        (
            "statement {year}-{month}",
            "transfer receipt {n}",
            "savings account {year}",
            "card statement {month}",
        ),
        (
            "Opening balance {amount} yen, closing balance {amount} yen.",
            "A transfer of {amount2} yen was sent to the savings account.",
            "Interest of {small} yen was credited on the last business day.",
            "Card payment to the electricity company: {small} yen.",
            "The salary deposit arrived on the 25th.",
            "ATM withdrawal of {amount2} yen at the station branch.",
            "The account statement lists every deposit and withdrawal this month.",
            "The transfer fee of {small} yen was charged to the account.",
        ),
        ("銀行取引明細 {year}年{month}月", "振込明細 {n}"),
        (
            "月初残高は{amount}円、月末残高は{amount}円です。",
            "普通預金口座へ{amount2}円を振り込みました。",
            "給与の振込を確認しました。",
            "ATMで{amount2}円を引き出しました。",
        ),
    ),
    Topic(
        "Travel",
        "trips, flights, hotels and itineraries",
        (
            "{city} itinerary",
            "flight booking {city}",
            "hotel confirmation {city}",
            "{city} trip notes",
            "packing list {city}",
        ),
        (
            "Day {d}: take the train to {city} and walk around the old town.",
            "The flight to {city} departs at {time} from gate {gate}.",
            "Check-in at the hotel in {city} starts at 3 pm.",
            "The hotel invoice for {n} nights totals {amount2} yen including tax.",
            "Pack a rain jacket for the hike near {city}.",
            "Book the museum tickets in {city} a week ahead.",
            "The airport limousine bus to {city} station takes about an hour.",
            "Exchange some money at the airport for the first day in {city}.",
        ),
        ("{city_ja}旅行の計画", "{city_ja} 旅程"),
        (
            "{city_ja}行きの新幹線は{time}に出発します。",
            "ホテルのチェックインは15時からです。",
            "{city_ja}の美術館のチケットを予約しました。",
            "旅行の持ち物リストを確認する。",
        ),
        habit=Habit(
            0.35,
            ("hotel invoice {city}", "hotel receipt {nnnn}", "invoice {nnnn}"),
            (
                "Invoice for accommodation: {n} nights.",
                "Total amount due {amount2} yen including consumption tax of {small} yen.",
                "Payment by credit card; please keep this invoice for your records.",
                "Invoice number {nnnn}, issued on {date}.",
                "Please keep this invoice for your tax records.",
                "Breakfast and the service charge are included in the total.",
                "The amount was charged to the card ending in {nnnn}.",
            ),
        ),
    ),
    Topic(
        "Machine Learning/Unsupervised",
        "",
        (
            "k-means notes",
            "PCA lecture",
            "clustering exercise {n}",
            "DBSCAN paper notes",
            "dimensionality reduction",
        ),
        (
            "K-means assigns each point to the nearest centroid and then moves the centroids.",
            "Choose the number of clusters with the elbow method or the silhouette score.",
            "PCA projects the data onto the directions of maximal variance.",
            "DBSCAN groups dense regions and marks isolated points as noise.",
            "Hierarchical clustering builds a dendrogram of nested clusters.",
            "Gaussian mixture models give soft cluster assignments with the EM algorithm.",
            "t-SNE embeds high dimensional data in two dimensions for plotting.",
            "Standardize the features before running k-means.",
        ),
        ("教師なし学習 ノート {n}", "クラスタリング 演習"),
        (
            "k-means法は各点を最も近い重心に割り当てる。",
            "主成分分析はデータを分散が最大の方向に射影する。",
            "クラスタ数はエルボー法で決める。",
        ),
    ),
    Topic(
        "Machine Learning/Deep Learning",
        "neural networks",
        (
            "backpropagation",
            "CNN notes",
            "transformer paper notes",
            "training tricks",
            "deep learning lecture {n}",
        ),
        (
            "Backpropagation applies the chain rule to compute the gradients of the loss.",
            "Convolutional layers share their weights across positions in the image.",
            "The transformer uses self-attention instead of recurrence.",
            "Dropout disables random neurons during training to reduce overfitting.",
            "Batch normalization stabilizes the training of deep networks.",
            "The learning rate warms up and then decays during training.",
            "Train the network with stochastic gradient descent and momentum.",
            "The validation loss stopped improving after epoch {n}.",
        ),
        ("ディープラーニング 講義 {n}", "ニューラルネット ノート"),
        (
            "誤差逆伝播法で損失の勾配を計算する。",
            "畳み込み層は画像の位置ごとに重みを共有する。",
            "過学習を防ぐためにドロップアウトを使う。",
        ),
        habit=Habit(
            0.3,
            ("autoencoder notes", "representation learning {n}", "embedding clustering"),
            (
                "An autoencoder learns a compressed representation of the data without labels.",
                "Cluster the learned embeddings to group similar images.",
                "Contrastive learning pulls similar examples together in the embedding space.",
                "A neural network can reduce the dimensionality of the data instead of PCA.",
                "The latent space can be visualized with t-SNE.",
                "No labels are needed to train the encoder.",
            ),
        ),
    ),
    Topic(
        "Recipes",
        "cooking and baking",
        ("{dish} recipe", "{dish}", "weekend baking {n}", "dinner ideas {n}"),
        (
            "Preheat the oven to {temp} degrees.",
            "Mix the flour, sugar and eggs in a large bowl.",
            "Simmer the sauce for {minutes} minutes and season with salt.",
            "Chop the onions and fry them in butter until golden.",
            "Serve with rice and a green salad.",
            "Bake for {minutes} minutes until a skewer comes out clean.",
            "Let the dough rest in the fridge overnight.",
            "Add the vegetables and stir fry them over high heat.",
        ),
        ("{dish_ja}の作り方", "{dish_ja} レシピ"),
        (
            "だしを取り、豆腐とわかめを入れて味噌を溶く。",
            "玉ねぎを切って弱火で炒める。",
            "オーブンを{temp}度に予熱する。",
            "砂糖としょうゆで味を調える。",
        ),
    ),
    Topic(
        "Health",
        "doctor visits, prescriptions, health checkups",
        (
            "health checkup {year}",
            "prescription {month}",
            "dental appointment",
            "blood test results {year}",
            "clinic receipt {month}",
        ),
        (
            "Blood pressure {bp}; cholesterol is within the normal range.",
            "Take one tablet after each meal for {days} days.",
            "The next dental checkup is in {month}.",
            "The doctor recommended more exercise and less salt.",
            "The clinic receipt for the visit is {small} yen.",
            "Keep the receipts of medical expenses for the year.",
            "The pharmacy hands out the prescription after the consultation.",
            "Vaccination record: second dose received on {date}.",
        ),
        ("健康診断結果 {year}", "処方箋 {month}月"),
        (
            "血圧は{bp}で正常範囲です。",
            "毎食後に1錠を{days}日間服用してください。",
            "次回の歯科検診は{month}月です。",
            "医師から減塩と運動を勧められた。",
        ),
        habit=Habit(
            0.35,
            ("receipt {nnnn}", "medical receipt {year}", "pharmacy receipt {month}"),
            (
                "Receipt: total {small} yen, paid in cash.",
                "Keep this receipt for the medical expense deduction on your tax return.",
                "Insurance covered 70 percent of the cost.",
                "Receipt number {nnnn}, issued on {date}.",
                "The deductible amount for the year is {amount2} yen.",
                "Consumption tax is not charged on this amount.",
            ),
        ),
    ),
    Topic(
        "University/Physics",
        "",
        (
            "lab report {n}",
            "mechanics homework {n}",
            "physics lecture notes {n}",
            "electromagnetism exam prep",
        ),
        (
            "Newton's second law relates force, mass and acceleration.",
            "The period of the pendulum depends on its length and on gravity.",
            "Measure the spring constant from the slope of force against extension.",
            "Energy is conserved in an elastic collision.",
            "The lab report must give the uncertainty of each measurement.",
            "Maxwell's equations describe the electric and magnetic fields.",
            "The homework asks for the trajectory of a projectile with air resistance.",
            "Plot the data and fit a straight line to find the slope.",
        ),
        ("物理学実験レポート {n}", "力学 演習 {n}"),
        (
            "ニュートンの運動方程式は力と加速度の関係を表す。",
            "振り子の周期は長さと重力加速度で決まる。",
            "測定値の不確かさをレポートに書くこと。",
        ),
    ),
    Topic(
        "University/History",
        "history lectures and essays",
        ("essay on {topic}", "history lecture {n}", "reading notes {n}", "seminar handout {n}"),
        (
            "The Meiji Restoration of 1868 ended the Tokugawa shogunate.",
            "The industrial revolution began in Britain in the late eighteenth century.",
            "The essay must cite at least three primary sources.",
            "Trade along the Silk Road connected China and the Mediterranean.",
            "The treaty ended the war and redrew the borders of Europe.",
            "The seminar discusses the causes of {topic}.",
            "The lecture compares the reforms of the two empires.",
            "Write a short summary of the reading before the seminar.",
        ),
        ("明治維新 レポート", "歴史学 講義 {n}"),
        (
            "明治維新により近代国家の建設が進められた。",
            "産業革命は18世紀後半のイギリスで始まった。",
            "レポートには一次史料を三つ以上引用すること。",
        ),
    ),
    Topic(
        "Home/Rental",
        "apartment lease, rent and landlord",
        (
            "lease agreement",
            "rent receipt {month}",
            "move-in checklist",
            "repair request {n}",
            "renewal notice {year}",
        ),
        (
            "The monthly rent of {amount2} yen is due on the 27th.",
            "The lease agreement runs for two years and renews automatically.",
            "Contact the landlord before making any repairs.",
            "The security deposit is returned after the move-out inspection.",
            "Rent is paid by bank transfer to the management company.",
            "The renewal fee is one month of rent.",
            "The repair request for the leaking faucet was sent to the landlord.",
            "Garbage is collected on Tuesdays and Fridays at the apartment.",
        ),
        ("賃貸契約書", "家賃 領収書 {month}月"),
        (
            "毎月の家賃{amount2}円は27日までに支払う。",
            "契約期間は2年間で自動更新される。",
            "修理の前に管理会社へ連絡すること。",
        ),
        habit=Habit(
            0.35,
            ("transfer {year}-{month}", "payment {month}", "standing order {year}"),
            (
                "Bank transfer of {amount2} yen to the account of the management company.",
                "Transfer date {date}; the fee of {small} yen was charged to the account.",
                "Standing order executed from the savings account.",
                "The balance after the transfer is {amount} yen.",
                "The transfer appears on the account statement for {month}.",
            ),
        ),
    ),
    Topic(
        "仕事",
        "会議の議事録と報告書",
        ("project status report {n}", "meeting notes {month}", "weekly report {n}"),
        (
            "The weekly status report lists the open tasks and their owners.",
            "Action items from the meeting are due on Friday.",
            "The project is on schedule for the release in {month}.",
            "The customer asked for a demo of the new feature.",
            "Share the slides with the team before the meeting.",
        ),
        ("議事録 {month}月", "週次報告 {n}", "業務報告書 {n}"),
        (
            "本日の会議では新製品のスケジュールを確認した。",
            "次回の打ち合わせは来週の水曜日に行う。",
            "担当者は資料を金曜日までに共有すること。",
            "プロジェクトの進捗は予定通りである。",
            "顧客からの要望を整理して報告する。",
            "来月のリリースに向けてテストを進める。",
        ),
    ),
)

# Sentences any document might contain, whatever its topic.
GENERIC = (
    "Please see the attached document for details.",
    "Updated on {date}.",
    "Contact me if you have any questions.",
    "Page {n} of 12.",
    "This document is for personal use only.",
)
GENERIC_JA = ("以上、よろしくお願いいたします。", "詳細は添付資料を参照してください。")
# Text that says nothing about the topic, found in many documents.
BOILERPLATE = (
    "This document was generated automatically; please do not reply to this message.",
    "All rights reserved. Copying this document without permission is prohibited.",
    "If you have any questions, contact the support desk on weekdays from 9 am to 5 pm.",
    "Thank you for your continued support.",
    "The information in this document is accurate as of the date of issue.",
)
BOILERPLATE_JA = (
    "本書の無断転載を禁じます。",
    "お問い合わせは平日9時から17時まで受け付けております。",
    "今後ともよろしくお願い申し上げます。",
)
JUNK_NAMES = (
    "scan_{nnnn}",
    "document({n})",
    "IMG_{nnnn}",
    "download ({n})",
    "untitled {n}",
    "スキャン_{nnnn}",
)
FILLERS = {
    "year": ("2021", "2022", "2023", "2024", "2025"),
    "month": tuple(f"{month:02d}" for month in range(1, 13)),
    "amount": ("1,234,000", "2,480,500", "980,000", "3,150,000", "4,020,000"),
    "amount2": ("12,500", "48,000", "7,800", "126,400", "33,600", "85,000"),
    "small": ("1,280", "3,450", "860", "5,200", "2,140"),
    "city": ("Kyoto", "Osaka", "Sapporo", "Fukuoka", "Lisbon", "Seoul", "Taipei"),
    "city_ja": ("京都", "大阪", "札幌", "福岡", "沖縄"),
    "n": tuple(str(number) for number in range(1, 13)),
    "nnnn": ("0034", "1182", "2041", "0457", "3310", "0921"),
    "dish": ("banana bread", "curry", "pancakes", "lasagna", "apple pie", "ramen"),
    "dish_ja": ("肉じゃが", "カレー", "味噌汁", "親子丼", "お好み焼き"),
    "temp": ("170", "180", "200", "220"),
    "minutes": ("10", "15", "20", "40"),
    "days": ("5", "7", "14"),
    "bp": ("118/76", "124/80", "132/84"),
    "time": ("08:15", "10:40", "13:05", "18:30"),
    "gate": ("12", "27", "45", "B3"),
    "d": ("1", "2", "3"),
    "topic": (
        "the Meiji Restoration",
        "the industrial revolution",
        "the Silk Road",
        "the Cold War",
        "the French Revolution",
    ),
    "date": ("2024-03-14", "2025-01-20", "2023-11-02"),
}
JAPANESE_SHARE = 0.3
JUNK_SHARE = 0.25
# Folders hold different numbers of files (relative to files_per_folder), as real ones do:
# a nearly empty folder is judged mostly by its name and description.
SIZES = (1.5, 1.0, 1.0, 0.25, 1.25, 1.0, 0.5, 0.33, 1.0, 0.75, 1.0)


class _Slots(dict):
    """Fills each {slot} of a template with a random value."""

    def __init__(self, rng: random.Random) -> None:
        super().__init__()
        self._rng = rng

    def __missing__(self, key: str) -> str:
        return self._rng.choice(FILLERS[key])


def _fill(template: str, rng: random.Random) -> str:
    return template.format_map(_Slots(rng))


def _document(topic: Topic, japanese: bool, rng: random.Random) -> tuple[str, str]:
    """(file name stem, text) of one generated file."""
    habit = topic.habit if topic.habit and rng.random() < topic.habit.share else None
    if habit is not None:
        japanese = False
        names, sentences = habit.names, habit.sentences
    else:
        names = topic.names_ja if japanese else topic.names
        sentences = topic.sentences_ja if japanese else topic.sentences
    title = _fill(rng.choice(names), rng)
    picked = rng.sample(sentences, k=min(len(sentences), rng.randint(3, 6)))
    generic = GENERIC_JA if japanese else GENERIC
    picked += [_fill(rng.choice(generic), rng) for _ in range(rng.randint(0, 2))]
    boilerplate = BOILERPLATE_JA if japanese else BOILERPLATE
    picked += rng.sample(boilerplate, k=rng.randint(0, 2))
    rng.shuffle(picked)
    body = (" " if not japanese else "").join(_fill(sentence, rng) for sentence in picked)
    stem = _fill(rng.choice(JUNK_NAMES), rng) if rng.random() < JUNK_SHARE else title
    return stem, f"{title}\n\n{body}\n"


def make_corpus(root: Path, *, files_per_folder: int = 12, seed: int = 7) -> dict[str, str]:
    """Write the sample library under ``root``; returns the folder descriptions to set."""
    rng = random.Random(seed)
    descriptions = {}
    for topic, size in zip(TOPICS, SIZES, strict=True):
        folder = root.joinpath(*topic.folder.split("/"))
        folder.mkdir(parents=True, exist_ok=True)
        if topic.description:
            descriptions[topic.folder] = topic.description
        for _ in range(max(2, round(files_per_folder * size))):
            japanese = bool(topic.sentences_ja) and rng.random() < JAPANESE_SHARE
            stem, text = _document(topic, japanese, rng)
            suffix = ".md" if rng.random() < 0.3 else ".txt"
            path = folder / f"{stem}{suffix}"
            number = 2
            while path.exists():  # same generated name twice: like a downloaded copy
                path = folder / f"{stem} ({number}){suffix}"
                number += 1
            path.write_text(text, encoding="utf-8")
    return descriptions
