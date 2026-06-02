# 火山引擎TTS自定义音色合成

这是一个使用火山引擎TTS服务进行自定义音色语音合成的Python脚本。

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置说明

在使用前，请先修改 `custom_tts.py` 中的以下配置：

1. **认证信息**：
   - `AK`: 你的火山引擎 Access Key ID
   - `SK`: 你的火山引擎 Access Key Secret

2. **音色配置**：
   - `VoiceType`: 你的自定义音色ID

## 使用方法

1. 安装依赖
2. 配置认证信息和音色ID
3. 运行脚本：

```bash
python custom_tts.py
```

脚本会生成一个名为 `custom_voice_output.mp3` 的音频文件。

## 参数说明

| 参数 | 说明 | 可选值 |
|------|------|--------|
| Text | 要合成的文本 | 任意字符串 |
| VoiceType | 音色ID | 自定义音色ID |
| CodecType | 音频格式 | mp3/wav/pcm |
| SampleRate | 采样率 | 8000/16000/24000 |
| Speed | 语速 | -5~5，0为默认 |
| Volume | 音量 | -10~10，0为默认 |
| Pitch | 音调 | -5~5，0为默认 |
| EnableSubtitle | 是否返回字幕 | True/False |

## 注意事项

- 请确保你的火山引擎账号已开通TTS服务
- 请确保你已创建了自定义音色并获取了音色ID
- 网络连接正常，能够访问火山引擎API
