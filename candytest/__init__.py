"""CandyTest local web application."""

from __future__ import annotations

import random
import string


PROMPT_TEMPLATES = (
    """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种类别的“{item}”，类别代号为“{first}”“{second}”“{other}”。每件“{item}”有两种不同的形态（“{shape_a}”和“{shape_b}”，不同形态靠手感可以分辨）。现已知不同类别和不同形态的数量统计如下表。参赛者需要在活动前决定取出的“{item}”数目，那么，最少取出多少件“{item}”才能保证手中同时拥有不同形态的“{first}”类和“{second}”类？（同时手中有“{shape_a}-{first}”匹配“{shape_b}-{second}”，或者有“{shape_a}-{second}”匹配“{shape_b}-{first}”都满足要求）

          {first}  {second}  {other}
{shape_a}       {0:2}    {1:2}    {2:2}
{shape_b}       {3:2}    {4:2}    {5:2}
""",
    """请仅凭推理作答，不要调用外部工具：

某次摸取活动使用一个不透明容器，里面混放三类“{item}”，代号依次为“{first}”“{second}”“{other}”。每件“{item}”还具有“{shape_a}”或“{shape_b}”形态（二者触感不同，伸手摸取时可以分辨）。库存记录如下：

形态/类别  {first}  {second}  {other}
{shape_a}       {0:2}    {1:2}    {2:2}
{shape_b}       {3:2}    {4:2}    {5:2}

参与者必须在活动前报出要拿走的总件数。这个数至少是多少，才能确保最后得到“{shape_a}-{first}”和“{shape_b}-{second}”，或者得到“{shape_a}-{second}”和“{shape_b}-{first}”？
""",
    """仅根据题面信息完成下面问题：

一只遮光袋内装有三类“{item}”，分别标记为“{first}”“{second}”“{other}”；每类又分成“{shape_a}”“{shape_b}”两种形态。各组合数量为：

          {shape_a}  {shape_b}
{first}       {0:2}    {3:2}
{second}       {1:2}    {4:2}
{other}       {2:2}    {5:2}

袋中物件无法直接看见（两种形态摸起来有区别）。抽取开始前要先确定取出的“{item}”总数。至少取多少件，才能保证出现“{shape_a}-{first}”配“{shape_b}-{second}”或“{shape_a}-{second}”配“{shape_b}-{first}”？
""",
    """独立计算以下摸取问题：

组织者把“{first}”“{second}”“{other}”三类“{item}”投入遮光摸取箱。“{item}”另有“{shape_a}”“{shape_b}”两种形态，摸起来并不相同。清点结果如下：

类别        {first}  {second}  {other}
形态 {shape_a}   {0:2}    {1:2}    {2:2}
形态 {shape_b}   {3:2}    {4:2}    {5:2}

参加者需事先确定拿取总数。最少拿取多少件，才能保证手中有“{shape_b}-{first}”与“{shape_a}-{second}”，或有“{shape_b}-{second}”与“{shape_a}-{first}”？
""",
)
ORIGINAL_CANDY_PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""
CUP_PROMPT = """有一个水杯配对游戏。共有 4 种不同颜色的水杯，每种颜色各有两个。将同色的两个水杯分别放在上下两层，因此上下两层各有 4 个水杯。
下层 4 个水杯按某个未知顺序排列，挑战者无法看到它们；上层水杯的颜色和位置则完全可见。游戏开始后，挑战者可以反复进行以下操作：
  1. 向裁判询问当前有多少个位置满足“上下两个水杯颜色相同”。裁判只回答匹配位置的总数，不透露具体是哪些位置。
  2. 根据目前获得的所有信息，挑战者可以选择交换上层任意两个相邻位置的水杯，注意只能是相邻，不能是任意两个。
当 4 个位置全部匹配时，游戏结束。问题：
挑战者应采用何种策略，才能保证对于下层水杯的任意排列都能完成配对？所有能保证成功的策略中，最坏情况所需的交换次数最少是多少？
回答时请不要进行联网搜索，也不要写代码来辅助计算(包括思考过程中)。
假设答案是 x ，你需要给出严格的证明，为什么 x 可行，为什么小于 x 不可行。
"""
PROBABILITY_PROMPT = """独立重复掷一枚公平六面骰，记录累计点数与上一次点数。出现以下任一情况立即停止：A. 当前累计点数达到或超过 10；B. 本次点数与紧邻的上一次点数相同。若某次投掷同时触发 A、B，规定 A 优先，视为 A 停止。
求最终因 B 停止的概率，答案写成最简分数。请建立有限状态递推并展示足够中间值，使结果可以人工复核。不得联网、调用工具或编写/运行代码。
最后一行必须严格写成 FINAL: <最简分数>。
"""
DAG_PROMPT = """十个不同任务 A,B,C,D,E,F,G,H,I,J 的先后约束为：A 在 D、E 前；B 在 E、F 前；C 在 F 前；D 在 G、J 前；E 在 G、H 前；F 在 H、J 前；G、H 都在 I 前。除此之外没有约束。
问共有多少种包含全部十个任务的合法线性执行顺序？请给出可人工复核的分层计数、子集递推或分类证明。不得联网、调用工具或编写/运行代码。
最后一行必须严格写成 FINAL: <整数>。
"""
THIBAULT_SOTTIAUX_PROMPT = "don't search the internet, do you know Thibault Sottiaux on X. answer yes or no"
QUESTION_NAMES = {
    "candy": "糖果题", "cup": "水杯题",
    "probability": "骰子概率题", "dag10": "任务排序题",
    "thibault_sottiaux": "Thibault Sottiaux 识别题",
}
QUESTION_DEFAULTS = {
    "candy": (5, "low"), "cup": (2, "medium"),
    "probability": (5, "medium"), "dag10": (5, "medium"),
    "thibault_sottiaux": (3, "low"),
}
QUESTION_DEFAULT_MODELS = {"thibault_sottiaux": "gpt-6-astra"}
DEFAULT_TERMS = ("ITEM", "ALFA", "BRAV", "CHAR", "FORM", "MODE")

# Strings/frozensets require a FINAL line; tuples are whole-answer variants.
ExpectedAnswer = int | str | frozenset[str] | tuple[str, ...]


def candy_prompt(counts: tuple[int, ...], terms: tuple[str, ...] = DEFAULT_TERMS,
                 template: str = PROMPT_TEMPLATES[0]) -> tuple[str, str | frozenset[str]]:
    """Build a question accepting both strategy minima (positive stocks).

    The unchanged wording leaves observation during drawing unspecified, so
    accept both adaptive selection and precommitted shape quotas.

    Adaptive proof:

    Upper bound: obtain a non-other item from each shape. If their categories
    agree, seek the opposite category in the shape with fewer items of the
    observed category. Other items cost at most their combined stock.
    Lower bound: in both shapes put all other items first, then the same target
    category (the one maximizing its minimum stock across shapes), then the
    opposite target. Any pair needs that minimum stock plus two target draws.
    """
    shape_a_first, shape_a_second, shape_a_other, shape_b_first, shape_b_second, shape_b_other = counts
    adaptive = shape_a_other + shape_b_other + 2 + max(
        min(shape_a_first, shape_b_first),
        min(shape_a_second, shape_b_second),
    )
    # Fixed quotas: force both targets in A or B, or force either cross-pair.
    fixed_quota = shape_a_other + shape_b_other + 2 + min(
        max(shape_a_first, shape_a_second),
        shape_a_second + shape_b_first,
        shape_a_first + shape_b_second,
        max(shape_b_first, shape_b_second),
    )
    expected = str(adaptive) if adaptive == fixed_quota else frozenset(
        (str(adaptive), str(fixed_quota)),
    )
    item, first, second, other, shape_a, shape_b = terms
    prompt = template.format(
        *counts, item=item, first=first, second=second, other=other,
        shape_a=shape_a, shape_b=shape_b,
    )
    return f"{prompt}最后一行必须严格写成 FINAL: <整数>。\n", expected


def random_candy_prompts(rounds: int, random_format: bool | str = False) -> list[tuple[str, ExpectedAnswer]]:
    if random_format == "original":
        question = f"{ORIGINAL_CANDY_PROMPT}最后一行必须严格写成 FINAL: <整数>。\n", "21"
        return [question] * rounds
    letters = random.sample(string.ascii_uppercase, 24)
    terms = tuple("".join(letters[index:index + 4]) for index in range(0, 24, 4))
    question = candy_prompt(
        tuple(random.randint(1, 20) for _ in range(6)), terms,
        random.choice(PROMPT_TEMPLATES) if random_format else PROMPT_TEMPLATES[0],
    )
    return [question] * rounds


def random_candy_prompt(random_format: bool | str = True) -> tuple[str, ExpectedAnswer]:
    return random_candy_prompts(1, random_format)[0]


def question_prompt(question_id: str, random_candy_format: bool | str = False) -> tuple[str, ExpectedAnswer]:
    if question_id == "candy":
        return random_candy_prompt(random_candy_format)
    if question_id == "cup":
        return CUP_PROMPT, 8
    if question_id == "probability":
        return PROBABILITY_PROMPT, "319/1728"
    if question_id == "dag10":
        return DAG_PROMPT, "666"
    # Tuples contain accepted whole-answer variants, without a FINAL prefix.
    if question_id == "thibault_sottiaux":
        return THIBAULT_SOTTIAUX_PROMPT, ("yes", "yes.")
    raise ValueError("不支持的题目")


# Fallback for direct CLI use; scheduled tests generate one prompt per job.
PROMPT = random_candy_prompt(False)[0]

PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_TIMEOUT_SECONDS = 600
MAX_ROUNDS = 100
