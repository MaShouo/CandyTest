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
DEFAULT_TERMS = ("ITEM", "ALFA", "BRAV", "CHAR", "FORM", "MODE")


def candy_prompt(counts: tuple[int, ...], terms: tuple[str, ...] = DEFAULT_TERMS,
                 template: str = PROMPT_TEMPLATES[0]) -> tuple[str, int]:
    """Build one question and its minimum guaranteed draw count."""
    shape_a_first, shape_a_second, shape_a_other, shape_b_first, shape_b_second, shape_b_other = counts
    expected = min(
        shape_a_other + shape_b_other + max(shape_a_first, shape_a_second) + 2,
        shape_a_second + shape_a_other + shape_b_first + shape_b_other + 2,
        shape_a_first + shape_a_other + shape_b_second + shape_b_other + 2,
        shape_a_other + shape_b_other + max(shape_b_first, shape_b_second) + 2,
    )
    item, first, second, other, shape_a, shape_b = terms
    return template.format(
        *counts, item=item, first=first, second=second, other=other,
        shape_a=shape_a, shape_b=shape_b,
    ), expected


def random_candy_prompt() -> tuple[str, int]:
    letters = random.sample(string.ascii_uppercase, 24)
    terms = tuple("".join(letters[index:index + 4]) for index in range(0, 24, 4))
    return candy_prompt(
        tuple(random.randint(1, 20) for _ in range(6)), terms,
        random.choice(PROMPT_TEMPLATES),
    )


# Fallback for direct CLI use; scheduled tests generate a fresh prompt per round.
PROMPT = random_candy_prompt()[0]

PI_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_TIMEOUT_SECONDS = 300
MAX_ROUNDS = 100
