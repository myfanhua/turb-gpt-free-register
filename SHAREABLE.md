# 5102 可分享版本

这是 5102 项目的无数据分发包。包内不包含 .env、账号、邮箱池、Token、Cookie、验证码、日志、任务记录、运行产物、浏览器环境、虚拟环境或 Git 历史。

## 启动

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    cp .env.example .env
    # 编辑 .env，只填写使用者自己的配置
    PORT=5102 ./webui.sh start

默认地址：http://127.0.0.1:5102/。运行时产生的账号、邮箱、Token、Cookie、日志和任务记录只会写入使用者自己的本地目录。

联系方式： [加入 Telegram 群](https://t.me/mzaijlq)；[加入 QQ 群 952990450](https://qm.qq.com/cgi-bin/qm/qr?group_code=952990450)。
