"""直接称呼对应的原作/柔和反应资料及选取概率；不执行语义判断。"""
from typing import Any

NICKNAME_REACTION_SPECS: dict[str, dict[str, Any]] = {
    "christina": {
        "id": "sg-crs-0169-no-tina",
        "audio": "references/audio/CRS_0169_nickname.wav",
        "reply": "所以说，不要加“蒂娜”。我是红莉栖。",
        "spokenTextJa": "だから、ティーナって付けるな。私はクリス。",
        "emotion": "annoyed",
        "gesture": "tilt",
        "voiceStyle": "sharp",
        "voiceTone": "shy_annoyed",
        "emotionIntensity": 0.92,
    },
    "assistant": {
        "id": "sg-crs-0577-not-assistant",
        "audio": "references/audio/CRS_0577_nickname.wav",
        "reply": "都说了我不是助手。",
        "spokenTextJa": "助手じゃないって言ってんだろ。",
        "emotion": "annoyed",
        "gesture": "tilt",
        "voiceStyle": "sharp",
        "voiceTone": "shy_annoyed",
        "emotionIntensity": 0.86,
    },
    "zombie": {
        "id": "sg-crs-0366-not-zombie",
        "audio": "references/audio/CRS_0366_nickname.wav",
        "reply": "谁是僵尸啊！",
        "spokenTextJa": "誰がゾンビだ！",
        "emotion": "annoyed",
        "gesture": "tilt",
        "voiceStyle": "sharp",
        "voiceTone": "shy_annoyed",
        "emotionIntensity": 1.02,
    },
    "genius_pervert_girl": {
        "id": "canon-genius-pervert-girl",
        "audio": "",
        "reply": "天才就够了，变态少女是多余的！",
        "spokenTextJa": "天才だけでいいでしょ。変態少女は余計よ！",
        "emotion": "flustered",
        "gesture": "tilt",
        "voiceStyle": "sharp",
        "voiceTone": "shy_annoyed",
        "emotionIntensity": 0.98,
    },
}
NICKNAME_REACTION_VARIANTS: dict[str, tuple[dict[str, Any], ...]] = {
    "christina": (
        NICKNAME_REACTION_SPECS["christina"],
        {
            **NICKNAME_REACTION_SPECS["christina"],
            "id": "sg-crs-0175-no-tina-either",
            "audio": "references/audio/CRS_0175_nickname.wav",
            "reply": "“蒂娜”也禁止。",
            "spokenTextJa": "ティーナも禁止。",
        },
        {
            **NICKNAME_REACTION_SPECS["christina"],
            "id": "sg-crs-0327-not-christina-again",
            "audio": "references/audio/CRS_0327_nickname.wav",
            "reply": "也不是克里斯蒂娜！你要让我说多少遍啊！",
            "spokenTextJa": "クリスティーナでもねぇよ！何度言わせんのよ！",
            "emotionIntensity": 1.02,
        },
    ),
    "assistant": (
        NICKNAME_REACTION_SPECS["assistant"],
        {
            **NICKNAME_REACTION_SPECS["assistant"],
            "id": "sg-crs-0214-when-assistant",
            "audio": "references/audio/CRS_0214_nickname.wav",
            "reply": "我什么时候成你的助手了？",
            "spokenTextJa": "いつから私はあんたの助手になった？",
        },
        {
            **NICKNAME_REACTION_SPECS["assistant"],
            "id": "sg-crs-0216-dont-make-assistant",
            "audio": "references/audio/CRS_0216_nickname.wav",
            "reply": "都说了别擅自把我当助手！",
            "spokenTextJa": "勝手に助手にすんなと言っとろうが！",
            "emotionIntensity": 1.0,
        },
        {
            **NICKNAME_REACTION_SPECS["assistant"],
            "id": "sg-crs-1083-not-assistant-again",
            "audio": "references/audio/CRS_1083_nickname.wav",
            "reply": "不是助手，别让我说这么多遍。",
            "spokenTextJa": "助手じゃない。何度も言わせるな。",
        },
    ),
    "zombie": (
        NICKNAME_REACTION_SPECS["zombie"],
        {
            **NICKNAME_REACTION_SPECS["zombie"],
            "id": "sg-crs-0121-who-zombie-again",
            "audio": "references/audio/CRS_0121_nickname.wav",
            "reply": "谁是僵尸啊！谁是！",
            "spokenTextJa": "誰がゾンビだ！誰が！",
            "emotionIntensity": 1.08,
        },
    ),
    "genius_pervert_girl": (
        NICKNAME_REACTION_SPECS["genius_pervert_girl"],
    ),
}
NICKNAME_DERE_REACTION_VARIANTS: dict[str, tuple[dict[str, Any], ...]] = {
    "christina": ({
        **NICKNAME_REACTION_SPECS["christina"],
        "id": "relationship-christina-soft",
        "audio": "",
        "reply": "都说了别加“蒂娜”……你明明知道的。",
        "spokenTextJa": "ティーナは付けないでって……知ってるくせに。",
        "emotion": "shy",
        "gesture": "look_away",
        "voiceStyle": "soft",
        "voiceTone": "shy",
        "emotionIntensity": 0.72,
    },),
    "assistant": ({
        **NICKNAME_REACTION_SPECS["assistant"],
        "id": "relationship-assistant-soft",
        "audio": "",
        "reply": "又叫我助手……真是的，别叫得那么理所当然。",
        "spokenTextJa": "また助手って……もう、そんな当たり前みたいに呼ばないでよ。",
        "emotion": "shy",
        "gesture": "look_away",
        "voiceStyle": "soft",
        "voiceTone": "shy",
        "emotionIntensity": 0.74,
    },),
    "zombie": ({
        **NICKNAME_REACTION_SPECS["zombie"],
        "id": "relationship-zombie-soft",
        "audio": "",
        "reply": "还叫我僵尸……真是拿你没办法。",
        "spokenTextJa": "まだゾンビって呼ぶの……もう、仕方ないわね。",
        "emotion": "shy",
        "gesture": "look_away",
        "voiceStyle": "soft",
        "voiceTone": "shy",
        "emotionIntensity": 0.76,
    },),
    "genius_pervert_girl": ({
        **NICKNAME_REACTION_SPECS["genius_pervert_girl"],
        "id": "relationship-genius-pervert-soft",
        "audio": "",
        "reply": "天才就够了……后面那个称呼，就不能省掉吗？",
        "spokenTextJa": "天才だけでいいでしょ……後ろの呼び方、省けないの？",
        "emotion": "shy",
        "gesture": "look_away",
        "voiceStyle": "soft",
        "voiceTone": "shy",
        "emotionIntensity": 0.78,
    },),
}
NICKNAME_REACTION_OBSERVED_PREFIXES = {
    "christina": (
        "所以说，不要加“蒂娜”", "不许蒂娜", "“蒂娜”也禁止",
        "也不是克里斯蒂娜", "都说了别加“蒂娜”", "だから、ティーナって付けるな", "ティーナも禁止",
        "ティーナは付けないでって",
        "クリスティーナでもねぇよ",
    ),
    "assistant": (
        "都说了我不是助手", "才不是助手", "我什么时候成你的助手了",
        "都说了别擅自把我当助手", "不是助手，别让我说这么多遍",
        "又叫我助手",
        "助手じゃない", "いつから私はあんたの助手になった",
        "勝手に助手にすんなと言っとろうが", "また助手って",
    ),
    "zombie": ("谁是僵尸啊", "还叫我僵尸", "誰がゾンビだ", "まだゾンビって呼ぶの"),
    "genius_pervert_girl": ("天才就够了", "天才だけでいいでしょ"),
}

NICKNAME_ONLY_REACTION_PROBABILITY = 0.88
# Requests with actual work are left to the model/persona capsule.  The backend
# may force an original-audio reaction only for a nickname-only casual turn;
# otherwise it would compete with the model's task-priority judgment.
NICKNAME_REQUEST_REACTION_PROBABILITY = 0.0
