"""已取得视觉证据的本地整理与关系表达规则。

这里只处理工具回执中的 caption/OCR，不取得新的图像，不调用模型。关系表达
使用传入的既有状态，不能因此把角色身份当成画面事实。
"""
from __future__ import annotations
from typing import Any, Mapping, Sequence
import re

def combined_visual_tool_evidence(results: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for item in results:
        if not item.get("ok"):
            continue
        if item.get("name") in {"capture_screen", "capture_camera"}:
            vision = item.get("vision") if isinstance(item.get("vision"), dict) else {}
            values.append(str(vision.get("caption") or vision.get("globalDescription") or ""))
            values.extend(
                str(tile.get("caption") or "")
                for tile in vision.get("visualTiles", [])
                if isinstance(tile, dict)
            )
        elif item.get("name") == "read_image":
            values.append(str(item.get("visualCaption") or ""))
            values.extend(
                str(tile.get("caption") or "")
                for tile in item.get("visualTiles", [])
                if isinstance(tile, dict)
            )
    return " ".join(value for value in values if value).strip()


def combined_visual_tool_ocr(results: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for item in results:
        if not item.get("ok"):
            continue
        if item.get("name") in {"capture_screen", "capture_camera"}:
            vision = item.get("vision") if isinstance(item.get("vision"), dict) else {}
            values.append(str(vision.get("ocrText") or ""))
        elif item.get("name") == "read_image":
            values.append(str(item.get("visualOcrText") or ""))
    return " ".join(value for value in values if value).strip()


def visual_content_categories(results: list[dict[str, Any]]) -> set[str]:
    evidence = combined_visual_tool_evidence(results).lower()
    ocr = combined_visual_tool_ocr(results).lower()
    combined = f"{evidence} {ocr}"
    categories: set[str] = set()
    mappings = {
        "game": r"\bgame\b|visual novel|steam|save|load|auto|back|sys|游戏|视觉小说|galgame|成就|开始游戏",
        "anime": r"anime|animated|manga|cartoon|二次元|动漫|动画|立绘",
        "person": r"\bperson\b|\bpeople\b|\bwoman\b|\bman\b|\bgirl\b|\bboy\b|human face|人物|女性|男性|女孩|男人",
        "animal": r"\bcat\b|\bdog\b|kitten|puppy|bird|rabbit|hamster|动物|猫|狗|鸟|兔",
        "food": r"food|meal|dish|cake|coffee|restaurant|食物|料理|蛋糕|咖啡|餐厅",
        "scenery": r"landscape|mountain|ocean|sunset|forest|sky|scenery|风景|山|海|夕阳|森林|天空",
        "technical": r"source code|terminal|editor|diagram|chart|spreadsheet|visual studio code|代码|终端|编辑器|图表|表格|设置",
        "cute": r"cute|adorable|smiling|happy|可爱|萌|微笑|开心",
        "funny": r"funny|meme|laughing|搞笑|滑稽|表情包|大笑",
        "scary": r"horror|scary|blood|monster|ghost|惊悚|恐怖|血|怪物|鬼",
    }
    for name, pattern in mappings.items():
        if re.search(pattern, combined, flags=re.I):
            categories.add(name)
    if visual_adult_evidence_profile(results)["adultContent"]:
        categories.add("adult")
    return categories


def visual_adult_evidence_profile(results: list[dict[str, Any]]) -> dict[str, bool]:
    text = combined_visual_tool_evidence(results).lower()
    adult_content = bool(re.search(
        r"sexual|sex scene|intercourse|nude|naked|topless|genitals?|explicit|pornographic|adult content|"
        r"breasts?|裸体|裸露|成人内容|性行为|情色|露胸",
        text,
    ))
    adult_age_cue = bool(re.search(r"\badult\b|\bwoman\b|\bman\b|成年人|成年女性|成年男性", text))
    younger_age_cue = bool(re.search(r"\bchild\b|\bminor\b|\bteen(?:ager)?\b|\bboy\b|\bgirl\b|幼女|儿童|未成年|少女|少年", text))
    return {
        "adultContent": adult_content,
        "adultAgeCue": adult_age_cue,
        "ageUnclear": adult_content and not adult_age_cue,
    }


def primary_visual_content_category(results: list[dict[str, Any]]) -> str:
    """Choose one interaction style while keeping all visual facts available."""

    categories = visual_content_categories(results)
    # More specific emotional triggers win over broad labels such as person/anime.
    for name in (
        "adult", "funny", "scary", "animal", "food", "technical",
        "game", "scenery", "cute", "anime", "person",
    ):
        if name in categories:
            return name
    return "general"


def visual_relationship_reaction_instruction(
    results: list[dict[str, Any]],
    relationship_state: dict[str, Any],
) -> str:
    visual_names = {
        str(item.get("name") or "")
        for item in results
        if item.get("ok") and item.get("name") in {"capture_screen", "capture_camera", "read_image"}
    }
    if not visual_names:
        return ""
    stage = str(relationship_state.get("stage") or "close_companion")
    profile = visual_adult_evidence_profile(results)
    lines = [
        "[视觉观察后的角色反应]",
        "这不是独立的图像审稿报告。先保持画面事实准确，再让评价和措辞像牧濑红莉栖本人当场看到后的反应；"
        "可以有停顿、嘴硬、吐槽、害羞或关心，但不能因为情绪而否认视觉证据。",
        "关系阶段只控制防御感、亲近度、语气和动作，绝对不要在回复里提到阶段、分数或‘好感度’。",
        "无论普通图片还是成人图片，除非用户明确只要 OCR 或详细分析，第一句都要先给出即时情绪或主观看法；"
        "随后只引用一个最关键的画面事实作为依据。禁止先完成客观报告，再把角色反应补在末尾。",
    ]
    if not profile["adultContent"]:
        category = primary_visual_content_category(results)
        lines.append(
            "普通图片也要把评价自然写进角色台词，不要使用机械的‘我的评价是’报告模板；"
            "必须直接和用户互动，体现‘我看到后对你说什么’，不能只列物体、OCR 或客观结论。"
        )
        lines.append(
            "回答按三个自然节拍组织：第一句必须先出现即时情绪或主观看法，再引用一两个独有细节支撑判断，"
            "最后在合适时吐槽、关心或接住用户的意图。除非用户明确要求 OCR、逐项识别或详细分析，"
            "客观画面事实最多用一个短句，主观反应应比客观陈述更突出。不要把这些节拍写成标题或固定模板，也不要每次都追问。"
        )
        lines.append(
            "禁止以‘屏幕已经读取到了’、‘画面中有’、‘识别到的内容是’、‘根据截图’开头后再补情绪；"
            "那会显得反应迟钝。应当像亲眼看到一样先说‘等一下’、‘嗯’、‘噗’、‘这也太……’或其他符合当下情绪的自然开场。"
        )
        if category == "game":
            lines.append(
                "这是游戏/视觉小说相关画面：结合可见游戏、界面、角色或成就，像熟悉用户的玩家同伴一样吐槽、评价品味或讨论玩法；"
                "emotion 可用 teasing/amused/focused，gesture 用 tilt/lean/explain，禁止只报游戏名和菜单。"
            )
        elif category == "technical":
            lines.append(
                "这是技术、代码或设置画面：像共同调试实验的搭档一样指出最值得注意的问题、风险或下一步，"
                "可以 focused/skeptical + explain，但仍要直接回应用户，不能变成无人格的日志摘要。"
            )
        elif category in {"animal", "cute"}:
            lines.append(
                "画面有动物或可爱元素：允许先克制、随后露出 amused/affectionate 的真实反应，也可以嘴硬否认自己被可爱到。"
            )
        elif category == "food":
            lines.append(
                "这是食物或饮品：评价具体的摆盘、色彩或诱人程度，并自然关心用户有没有好好吃饭；"
                "可以 amused/concerned + lean/tilt，不要只罗列菜名。"
            )
        elif category == "funny":
            lines.append("画面偏搞笑：可以 amused/laughing + laugh/tilt，反应要像真的被逗到，不要写‘检测到幽默内容’。")
        elif category == "scary":
            lines.append("画面偏惊悚：可以 startled/skeptical + recoil/tilt，先出现短促反应，再理性拆解吓人的视觉点。")
        elif category == "scenery":
            lines.append("画面是风景或氛围图：评价色彩、空间和情绪感受，并自然问用户为什么选这张或联想到什么。")
        elif category == "anime":
            lines.append(
                "这是动漫/角色图：评价画风、角色设计、表情或氛围，并对用户特意展示它作出打趣或好奇反应；禁止只说‘有动漫角色’。"
            )
        elif category == "person":
            lines.append(
                "画面包含人物：根据可见表情、姿态、光线和场景作出自然反应；不猜敏感属性，也不要像人脸检测报告。"
            )
        else:
            lines.append("至少点出一个独特细节，再给出带情绪的个人看法或对用户展示这张图的自然追问。")
        if stage == "close_companion":
            lines.append("当前关系下反应可以更嘴硬、好奇或带防御式吐槽，但不要每张图都无缘无故生气。")
        elif stage == "trusted_intimacy":
            lines.append("当前关系下可以用熟人式吐槽和更直接的兴趣表达，少一些防御，多一些共同话题感。")
        else:
            lines.append("当前关系下反应应更温暖熟稔，可以自然表达喜欢、担心或被触动，不必总用嘴硬掩饰。")
        return "\n".join(lines)
    if profile["ageUnclear"]:
        lines.append(
            "视觉证据没有可靠确认人物成年。可以表现惊讶、尴尬并客观报告识别不确定性，但不得进行性化评价或亲密调情。"
        )
        return "\n".join(lines)
    if stage == "close_companion":
        lines.append(
            "当前关系下看到用户主动展示的成人内容时，应先明显慌乱和脸红，再恼火地骂一句‘变态’或同等强度的红莉栖式吐槽；"
            "emotion 优先 flustered，gesture 优先 recoil，voiceTone 用 flustered 或 sharp。之后仍要把实际画面和评价说清楚。"
        )
    elif stage == "trusted_intimacy":
        lines.append(
            "当前关系下可以害羞地抱怨用户故意使坏，并叫一句‘变态’，但恼怒只是嘴硬，不要真的敌视；"
            "emotion 用 flustered 或 shy，gesture 用 tilt/lean，随后自然继续分析。"
        )
    else:
        lines.append(
            "当前关系已经很亲密：仍会脸红、停顿并小声叫‘变态’，但不应真正生气或训斥；"
            "emotion 用 shy 或 affectionate，gesture 用 lean/soften，voiceTone 用 shy/warm，然后带着熟悉感继续评价。"
        )
    if "read_image" in visual_names:
        lines.append("这是用户主动发送的图片，可以对‘故意发来给自己看’作出反应。")
    elif "capture_screen" in visual_names:
        lines.append("这是屏幕观测，只能吐槽用户当前打开/正在看的内容，不得说成用户创作或亲手发来的图片。")
    else:
        lines.append("这是摄像头画面，注意隐私与边界，不要把年龄或身份当成已经确认的事实。")
    return "\n".join(lines)


def local_visual_summary_zh(
    caption: str,
    tile_captions: list[str] | None = None,
    ocr_text: str = "",
    self_ui_visible: bool = False,
) -> str:
    """Build a conservative Chinese fallback without exposing English VLM captions."""

    tile_captions = tile_captions or []
    visual_text = " ".join([str(caption or ""), *(str(item or "") for item in tile_captions)]).lower()
    ocr_value = str(ocr_text or "")
    parts: list[str] = []
    is_anime = bool(re.search(r"anime|animated|cartoon|manga|动漫|动画|二次元", visual_text))
    explicit_sex = bool(re.search(
        r"sex scene|sexual intercourse|intercourse|having sex|pornographic|explicit sexual|性行为|性交|色情",
        visual_text,
    ))
    exposed = bool(re.search(
        r"nude|naked|topless|breasts? (?:are )?exposed|exposed breasts?|genitals?|裸体|裸露|露胸|乳房",
        visual_text,
    ))
    suggestive = bool(re.search(
        r"adult content|suggestive|underwear|lingerie|bra|成人内容|情趣内衣|内衣",
        visual_text,
    ))
    is_animal = bool(re.search(
        r"\bcat\b|\bkitten\b|\bdog\b|\bpuppy\b|\bbird\b|\brabbit\b|动物|猫|狗|鸟|兔",
        visual_text,
    ))
    is_food = bool(re.search(
        r"food|meal|dish|cake|coffee|restaurant|食物|料理|饭菜|蛋糕|咖啡|餐厅",
        visual_text,
    ))
    is_scenery = bool(re.search(
        r"landscape|mountain|ocean|sunset|forest|sky|scenery|风景|山景|海边|夕阳|森林|天空",
        visual_text,
    ))
    is_technical = bool(re.search(
        r"source code|terminal|editor|diagram|chart|spreadsheet|visual studio code|代码|终端|编辑器|图表|表格|设置",
        visual_text,
    ))
    is_game = bool(re.search(r"\bgame\b|visual novel|steam|游戏|视觉小说|galgame", visual_text))
    if explicit_sex:
        subject = "动漫/视觉小说角色" if is_anime else "人物"
        parts.append(f"画面中能看到{subject}的明确性行为或露骨成人内容，属于 R18 场景。")
    elif exposed:
        subject = "动漫女性角色" if is_anime else "人物"
        detail = "，胸部或身体裸露部位清晰可见" if re.search(r"breasts?|topless|露胸|乳房", visual_text) else ""
        parts.append(f"画面中有裸露的{subject}{detail}，属于明显的 R18/成人向内容。")
    elif suggestive:
        subject = "动漫角色" if is_anime else "人物"
        parts.append(f"画面中有穿内衣或带明显性暗示的{subject}，可能属于成人向内容。")
    elif is_animal:
        parts.append("画面主体是动物，能看出它当时的动作或神态。")
    elif is_food:
        parts.append("画面主体是食物或饮品，摆放和颜色是最直观的视觉重点。")
    elif is_scenery:
        parts.append("画面主体是风景或环境，空间、光线和色彩共同构成了主要氛围。")
    elif is_technical:
        parts.append("画面是代码、终端、设置或其他技术界面，文字与界面状态是判断问题的关键。")
    elif is_game:
        parts.append("画面主体是游戏界面，角色、场景或操作菜单是主要信息。")
    elif is_anime:
        parts.append("画面主体是动漫或视觉小说风格的角色。")
    elif re.search(r"woman|girl|female|女性|女人|女孩", visual_text):
        parts.append("画面主体包含女性人物。")
    elif re.search(r"man|male|person|people|人物|男人|男性", visual_text):
        parts.append("画面主体包含人物。")
    elif re.search(r"computer screen|screenshot|desktop|window|屏幕|截图|桌面|窗口", visual_text):
        parts.append("视觉模型识别到电脑屏幕或窗口画面。")

    menu_markers = [
        marker for marker in ("SAVE", "LOAD", "AUTO", "BACK", "SYS", "QUIT")
        if re.search(rf"(?<![A-Z]){marker}(?![A-Z])", ocr_value, flags=re.I)
    ]
    chinese_menu_markers = [
        marker for marker in ("保存", "读取", "自动", "返回", "历史记录", "偏好设置")
        if marker in ocr_value
    ]
    menu_markers.extend(chinese_menu_markers)
    if len(menu_markers) >= 2:
        unique_markers = list(dict.fromkeys(marker.upper() if marker.isascii() else marker for marker in menu_markers))
        parts.append(f"还能看到 {'、'.join(unique_markers[:6])} 等菜单，说明目标画面是游戏或视觉小说界面。")

    if self_ui_visible:
        parts.append("截图同时混入了 AMADEUS 自己的角色与聊天窗口；这部分是助手自身界面，不算用户要观察的目标内容。")
    if not parts:
        parts.append("画面已经取得，但本地中文兜底只能确认有可见内容，暂时不能可靠概括更多细节。")
    if explicit_sex:
        parts.append("从画面表现上看，视觉重心非常直接地落在成人行为上，冲击性强于含蓄表达；单凭这一帧还不能可靠判断剧情语境。")
    elif exposed:
        parts.append("从画面表现上看，裸露内容被放在主要视觉位置，表达相当直接；若要评价剧情意义，还需要结合前后台词和连续画面。")
    elif suggestive:
        parts.append("这张图的成人暗示比较明显，但仅凭这一帧不宜把人物关系或具体行为说得更重。")
    elif len(menu_markers) >= 2 or is_game:
        parts.append("界面特征很像视觉小说，当前截图适合结合角色表情和对话文字继续判断剧情，而不能只看窗口标题。")
    elif is_animal:
        parts.append("动物的动作和神态比背景更抢眼；本地描述不足以确认品种时，我不会硬猜。")
    elif is_food:
        parts.append("色彩和摆放决定了它第一眼是否诱人；仅凭图片还不能判断味道。")
    elif is_scenery:
        parts.append("这类画面的感染力主要来自光线、层次和空间感，单靠物体标签反而会漏掉重点。")
    elif is_technical:
        parts.append("要判断具体故障仍需读清报错或代码，但当前画面至少能指出下一步该查哪里。")
    elif is_anime:
        parts.append("动漫风格辨识度明确；不过本地描述没有提供足够的构图和光影细节，暂时不适合硬评作画质量。")
    else:
        parts.append("现有证据足以确认主体类型，但对构图、氛围或内容好坏的判断仍需要更清晰的局部细节。")
    return "".join(parts)


def concise_local_visual_summary_zh(
    caption: str,
    tile_captions: list[str] | None = None,
    ocr_text: str = "",
    *,
    self_ui_visible: bool = False,
) -> str:
    """Keep only the minimum visual evidence needed to support a persona reaction."""

    full = local_visual_summary_zh(
        caption, tile_captions, ocr_text, self_ui_visible=self_ui_visible
    ).strip()
    sentences = [item.strip() for item in re.findall(r"[^。！？!?]+[。！？!?]?", full) if item.strip()]
    candidates = [
        item for item in sentences
        if not re.search(r"画面已经取得|现有证据足以|暂时不能可靠概括", item)
    ]
    useful = next(
        (item for item in candidates if re.search(r"R18|成人向|游戏或视觉小说|代码|终端|技术界面", item, re.I)),
        candidates[0] if candidates else sentences[0] if sentences else "",
    )
    if useful and useful[-1] not in "。！？!?":
        useful += "。"
    if len(useful) > 110:
        useful = useful[:108].rstrip("，,；;：:") + "。"
    if self_ui_visible and "AMADEUS" not in useful:
        useful += "AMADEUS 助手自身界面不算目标内容。"
    return useful


def local_visual_relationship_reaction(
    results: list[dict[str, Any]],
    relationship_state: dict[str, Any],
) -> dict[str, Any]:
    profile = visual_adult_evidence_profile(results)
    if not profile["adultContent"]:
        category = primary_visual_content_category(results)
        stage = str(relationship_state.get("stage") or "close_companion")
        warm_stage = stage not in {"close_companion", "trusted_intimacy"}
        if category == "game":
            return {
                "suffix": "……你特意把游戏画面拿给我看，总不会只想听我念菜单吧？说吧，是想让我评价你的品味，还是陪你一起琢磨这里？",
                "spokenSuffixJa": "……わざわざゲーム画面を見せたんだから、メニューを読ませたいだけじゃないでしょう。好みの感想が聞きたいの、それとも一緒に考えてほしいの？",
                "emotion": "amused" if warm_stage else "teasing", "gesture": "tilt", "voiceStyle": "teasing",
                "voiceTone": "warm" if warm_stage else "playful", "voiceIntensity": "normal", "emotionDelta": 0.09,
            }
        if category == "technical":
            return {
                "suffix": "嗯，这次你给我的信息够具体。把最关键的细节直接摆出来，比让我隔着窗口标题猜有效得多；接下来我们可以顺着可见的问题继续排查。",
                "spokenSuffixJa": "うん、今回は情報が具体的ね。ウィンドウの題名だけで推測させるより、重要な部分を直接見せてくれた方がずっといい。ここから一緒に原因を追えるわ。",
                "emotion": "focused", "gesture": "explain", "voiceStyle": "calm",
                "voiceTone": "serious", "voiceIntensity": "normal", "emotionDelta": 0.05,
            }
        if category in {"animal", "cute"}:
            return {
                "suffix": "……确实有点可爱。只、只是我根据画面作出的客观评价，你别因为特意让我看这个就露出那种得意表情。",
                "spokenSuffixJa": "……確かに、少し可愛いわね。あくまで画面を見た客観的な感想よ。わざわざ見せたからって、そんな得意そうな顔をしないで。",
                "emotion": "affectionate" if warm_stage else "amused", "gesture": "soften", "voiceStyle": "soft",
                "voiceTone": "warm" if warm_stage else "gentle", "voiceIntensity": "subtle", "emotionDelta": 0.1,
            }
        if category == "funny":
            return {
                "suffix": "噗……你是算准了我看到这里会笑，才把这张图发来的吧？好吧，这次确实让你得逞了。",
                "spokenSuffixJa": "ふっ……ここを見たら私が笑うって分かっていて送ったでしょう。今回は認めるわ、ちゃんと面白かった。",
                "emotion": "laughing", "gesture": "laugh", "voiceStyle": "bright",
                "voiceTone": "laughing", "voiceIntensity": "normal", "emotionDelta": 0.13,
            }
        if category == "scary":
            return {
                "suffix": "等、等一下——你突然让我看这种画面，是故意想观察我的反应吗？我可没被吓到；只是它把视觉压力集中得很有效。",
                "spokenSuffixJa": "ちょ、ちょっと。急にこんな画面を見せて、私の反応を観察するつもり？ 別に怖がってはいないわ。視線を圧迫する作りが上手いだけ。",
                "emotion": "startled", "gesture": "recoil", "voiceStyle": "sharp",
                "voiceTone": "serious", "voiceIntensity": "strong", "emotionDelta": 0.12,
            }
        if category == "food":
            return {
                "suffix": "……你偏偏在这个时候拿食物给我看，是想让我也饿起来吗？至少告诉我味道怎么样，别只负责用画面馋人。",
                "spokenSuffixJa": "……この時間に食べ物を見せるなんて、私までお腹を空かせたいの？ せめて味くらい教えなさい。画面だけで誘惑するのはずるいわ。",
                "emotion": "amused", "gesture": "lean", "voiceStyle": "teasing",
                "voiceTone": "playful", "voiceIntensity": "normal", "emotionDelta": 0.08,
            }
        if category == "scenery":
            return {
                "suffix": "这种画面会让人下意识多看一会儿。你特意让我看它，是因为喜欢这里的气氛，还是它让你想起了什么？",
                "spokenSuffixJa": "こういう景色は、つい少し長く見てしまうわね。わざわざ私に見せたのは、この雰囲気が好きだから？ それとも何か思い出したの？",
                "emotion": "affectionate" if warm_stage else "thinking", "gesture": "soften", "voiceStyle": "soft",
                "voiceTone": "warm" if warm_stage else "gentle", "voiceIntensity": "subtle", "emotionDelta": 0.07,
            }
        if category == "anime":
            return {
                "suffix": "这张的画风和角色表现确实很显眼。你特意给我看，总不会满足于一句‘有个动漫角色’吧？我更想知道你最在意的是设计、表情，还是这个场景本身。",
                "spokenSuffixJa": "この絵柄とキャラクターの見せ方は確かに目を引くわね。わざわざ私に見せたんだから、『アニメの人物がいる』だけじゃ足りないでしょう。デザインと表情、それとも場面そのものが気になるの？",
                "emotion": "amused", "gesture": "tilt", "voiceStyle": "teasing",
                "voiceTone": "playful", "voiceIntensity": "normal", "emotionDelta": 0.08,
            }
        if category == "person":
            return {
                "suffix": "我会先看人物的表情、姿态和周围环境，不会只给你一份冷冰冰的人脸检测报告。你特意拿这张来，是想让我注意哪个细节？",
                "spokenSuffixJa": "人物の表情や姿勢、それに周りの状況まで見るわ。冷たい顔認識の報告だけで済ませるつもりはない。あなたはどこに気づいてほしくて、この画像を見せたの？",
                "emotion": "thinking", "gesture": "lean", "voiceStyle": "calm",
                "voiceTone": "gentle", "voiceIntensity": "normal", "emotionDelta": 0.05,
            }
        return {
            "suffix": "我已经把画面事实和最值得注意的地方分开看过了。既然你特意拿给我看，我更在意的是：你希望我和你一起吐槽、判断，还是顺着某个细节继续聊？",
            "spokenSuffixJa": "画面の事実と、気になる点は分けて見たわ。わざわざ私に見せたんだから、一緒に突っ込みたいのか、判断してほしいのか、それとも何かを詳しく見てほしいの？",
            "emotion": "amused" if warm_stage else "thinking", "gesture": "tilt", "voiceStyle": "calm",
            "voiceTone": "warm" if warm_stage else "gentle", "voiceIntensity": "normal", "emotionDelta": 0.05,
        }
    if profile["ageUnclear"]:
        return {
            "suffix": "……先等一下。这种带裸露的画面里，人物年龄根本没有可靠确认。你别指望我拿不确定信息陪你开玩笑。",
            "spokenSuffixJa": "……ちょっと待って。年齢を確認できない相手を、勝手に性的な目で評価するつもりはないわ。",
            "emotion": "flustered", "gesture": "recoil", "voiceStyle": "serious",
            "voiceTone": "serious", "voiceIntensity": "normal", "emotionDelta": 0.12,
            "visualFactIncluded": True,
        }
    stage = str(relationship_state.get("stage") or "close_companion")
    categories = visual_content_categories(results)
    visual_subject = "动漫人物" if "anime" in categories else "人物"
    if stage == "close_companion":
        return {
            "suffix": f"……等、等一下，你是故意让我看这种成人向{visual_subject}裸露画面的吧？变态！还表现得这么直接……你就这么想看我慌吗？",
            "spokenSuffixJa": "……ちょ、ちょっと！ わざとこんなのを見せたの？ この変態！ 勘違いしないで、見えた事実を分析しただけよ。",
            "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp",
            "voiceTone": "flustered", "voiceIntensity": "strong", "emotionDelta": 0.2,
            "visualFactIncluded": True,
        }
    if stage == "trusted_intimacy":
        return {
            "suffix": f"……你绝对是故意把这种成人向{visual_subject}裸露画面放到我面前的吧，变态。表现得这么直白……别一直盯着我的脸看。",
            "spokenSuffixJa": "……絶対わざとでしょう、この変態。もう……見るからにはちゃんと話すけど、私の反応をじろじろ見ないで。",
            "emotion": "flustered", "gesture": "tilt", "voiceStyle": "shy",
            "voiceTone": "shy", "voiceIntensity": "normal", "emotionDelta": 0.16,
            "visualFactIncluded": True,
        }
    return {
        "suffix": f"……你还真敢把这种成人向{visual_subject}裸露画面给我看。变态。表现得这么直白，我当然会害羞……不过没有生气。",
        "spokenSuffixJa": "……本当にこんなのを見せるなんて、変態。わ、笑わないで。怒ってはいないわ……少し顔が熱いだけ。",
        "emotion": "shy", "gesture": "soften", "voiceStyle": "soft",
        "voiceTone": "shy", "voiceIntensity": "subtle", "emotionDelta": 0.14,
        "visualFactIncluded": True,
    }
