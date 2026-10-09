# runtimes/

Prebuilt inference runtimes fetched with `python scripts/runtimes.py fetch <name>` are unpacked
here, one subdirectory per runtime. `_downloads/` is the download cache and can be deleted at any
time.

- Everything in this directory except this file is ignored by Git.
- Versions are pinned in `runtimes.lock.toml`; `python scripts/runtimes.py status` shows what is
  installed.
- See [docs/runtimes.md](../docs/runtimes.md).

---

`python scripts/runtimes.py fetch <名称>` 获取的预编译推理运行时解压在此目录下，每个运行时一个子目录。
`_downloads/` 是下载缓存，可以随时删除。

- 除本文件外，此目录下的内容均被 Git 忽略。
- 版本锁定在 `runtimes.lock.toml` 中；`python scripts/runtimes.py status` 可查看当前的安装情况。
- 详见 [docs/zh-CN/runtimes.md](../docs/zh-CN/runtimes.md)。
