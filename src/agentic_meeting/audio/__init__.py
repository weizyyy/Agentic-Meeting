"""入口音频处理。

* ``gain.py``：收音增强（自动增益），挂在 Pipecat 传输输入的 ``audio_in_filter`` 上，先于语音检测
  （docs/architecture.md §4，docs/pipecat-notes.md §3.1.1）。
* ``diagnose.py``：离线诊断（电平、语音检测占比），供 ``scripts/mic_check.py`` 和测试使用。
"""
