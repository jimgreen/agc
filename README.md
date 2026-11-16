# AGC controller

独立的自动发电控制（AGC）程序，支持交流侧、直流侧及交直流混合场景中的风电、光伏、储能、燃料电池、制氢负荷和柴发协同控制。

## 控制策略

- 决策只使用实时采集量，不依赖 p_fur 预测值。
- 新能源优先：先计算新能源接纳空间（含储能 SOC 约束），再进行小步试探调节。
- 功率缺额按“储能放电 → 燃料电池 → 柴发补偿”处理。
- 功率盈余按新能源消纳、储能充电和制氢吸收处理。
- 受控设备支持 p_step/p_dead（以及兼容别名）控制步长和死区，减少频繁调节。

## 文件

- agc_ctrl.py：AGC 控制器及命令行入口。
- efile_read.py：E-file 读取和写回支持。
- paths.py：独立仓库路径解析。
- model_real.e：可直接运行的 AC/DC 混合示例模型。

## 运行

在本目录执行：

    python agc_ctrl.py --help
    python agc_ctrl.py --model model_real.e --step

控制器默认从模型文件所在目录读取模型；可通过 --model 指定其他 E-file。运行前请确保实时测量源可用；model_real.e 仅用于接口和策略验证。

## 依赖

仅使用 Python 标准库。
