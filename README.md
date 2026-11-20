# dtbtool — DTS/DTB 编辑 · 编译 · 分析工具

一个**纯 Python 标准库**实现的设备树（Device Tree）工具，面向 Armbian / Linux 开发板，同时提供 **Windows 免安装** 体验。

无需 `pip` 安装任何依赖，无需 `dtc`（可选），即可完成：

- **编辑 DTS → 编译 DTB**（默认纯 DTS，也支持带 `#include` 的源码树 DTS）
- **DTB → DTS 反编译**
- **硬件信息分析**：板卡 / SoC / CPU / 内存 / 外设 / GPIO 引脚 / 引脚复用 / 时钟频率 / 中断
- **DTS 差异对比**：两份 DTS 并排对比，差异项标红（精确到改动的词），支持只看差异行
- **Web 图形界面**：浏览器里打开、编辑、编译、下载、分析、对比，一键完成
- 所有覆盖写回操作**自动备份**

---

## 目录结构

| 文件 | 说明 |
|------|------|
| `启动工具.bat` | Windows 一键启动器（自动寻找 Python，自动打开浏览器） |
| `dtbtool.py` | 主程序：CLI + 内置 Web 服务（v2.1.0） |
| `dtb_engine.py` | 纯 Python 的 DTB↔DTS 转换引擎 + DTS 预处理器（`#include`/`#define`/条件编译） |
| `dts_analyze.py` | 硬件分析引擎：从 DTS/DTB 提炼外设、引脚、频率、中断等信息 |
| `dts_diff.py` | DTS 差异对比引擎：行级对齐 + 词级高亮，CLI 标红 / Web 并排 |
| `install.sh` | Armbian / Debian 设备安装脚本（可选，装 `device-tree-compiler` 并注册命令/服务） |

---

## 快速开始

### Windows（推荐，免安装）

1. 确保已安装 **Python 3.8+**（安装时勾选 “Add Python to PATH”）
2. 把本目录整体拷贝到任意位置（`.bat`、`dtbtool.py`、`dtb_engine.py`、`dts_analyze.py` 需放在一起）
3. **双击 `启动工具.bat`**，浏览器会自动打开 `http://127.0.0.1:8080`
4. 在网页中：
   - 左侧「选择本地 .dts 文件」载入源码
   - 需要时切换模式（纯 DTS / 带 #include），上传 `.h` / `.dtsi` 头文件
   - 点 **「分析 DTS」** 查看板卡/外设/引脚/频率报告
   - 编辑后点 **「编译并下载 DTB」**，把下载的 `.dtb` 拷回 Armbian 覆盖即可

> Web 服务默认只监听 `127.0.0.1`，仅本机可访问。

### Armbian / Linux

```bash
# 安装（自动安装 device-tree-compiler 并把 dtbtool 装到 /usr/local/bin）
sudo sh install.sh

# 额外注册开机自启的 Web 服务
sudo sh install.sh --with-service
```

也可以直接用 Python 运行，无需安装：

```bash
python3 dtbtool.py check
python3 dtbtool.py web --port 8080
```

---

## 命令行用法

```
python dtbtool.py <命令> [选项]
```

| 命令 | 说明 |
|------|------|
| `check` | 检查运行环境（引擎、dtc 是否可用） |
| `list` | 列举系统中的 DTB 文件（`/boot/dtb*`、`/usr/lib/linux-image-*` 等） |
| `info <file.dtb>` | 解析并显示 DTB 头信息（节点/属性/compatible/保留内存区） |
| `decompile <in.dtb> -o <out.dts>` | DTB → DTS 反编译 |
| `compile <in.dts> -o <out.dtb>` | DTS → DTB 编译 |
| `analyze <file>` | **分析 DTS/DTB：板卡/外设/引脚/频率/中断** |
| `edit <file.dtb>` | 反编译 → 调用编辑器 → 校验 → 写回（自动备份） |
| `diff <a> <b>` | 对比两个 DTB 或 DTS（差异标红，精确到词） |
| `web` | 启动浏览器编辑界面 |

### 示例

```bash
# 分析硬件信息（文本报告 / JSON）
python dtbtool.py analyze board.dts
python dtbtool.py analyze board.dts --cpp        # 带 #include 的源码树 DTS
python dtbtool.py analyze board.dtb --json

# 编译（纯 DTS，内置引擎，无需 dtc）
python dtbtool.py compile board.dts -o board.dtb

# 编译带 #include 的源码树 DTS（内置引擎会自动运行预处理器，展开常见 dt-bindings 宏）
python dtbtool.py compile board.dts -o board.dtb -i ./include

# 覆盖设备上的 DTB 时自动备份原文件
python dtbtool.py compile board.dts -o /boot/dtb/board.dtb --install

# 指定头文件搜索目录 / 使用系统 dtc
python dtbtool.py compile board.dts -o board.dtb -i ./include
python dtbtool.py --engine dtc compile complex.dts -o board.dtb

# 对比两份 DTS/DTB（终端标红；--json 输出结构化结果）
python dtbtool.py diff old.dts new.dts
python dtbtool.py diff old.dts new.dts --no-color
python dtbtool.py diff old.dts new.dts --json
```

`diff` 选项：`--no-color` 关闭标红、`--color` 强制标红、`--json` 输出结构化 JSON。
支持 DTB 输入（会自动反编译为 DTS 再对比）。

全局选项：`--engine {python,dtc,auto}`，默认 `python`（内置，Windows 免安装）。

`analyze` 选项：`--cpp` 强制预处理、`--plain` 不预处理、`--json` 输出结构化 JSON。

---

## 硬件分析功能

`analyze` 会从设备树中提炼以下内容，CLI 以文本报告输出，Web 界面以卡片 + 表格可视化展示（支持一键复制文本报告）：

- **板卡 / SoC**：型号 `model`、厂商（全志 / 瑞芯微 / 晶晨 / 博通 …）、SoC 型号、CPU、主频、compatible
- **内存 / 保留区**：基地址、容量（自动换算成 GB/MB）、字节数
- **外设清单**：按 25+ 类分组（UART、I2C、SPI、MMC/SD、USB、以太网、WiFi、蓝牙、显示、GPU、音频、GPIO、PWM、ADC、触控、LED、时钟、中断、电源等），每项含 compatible、状态（`okay`/`disabled`）、寄存器地址与范围、关键参数
- **GPIO 引脚使用**：哪个节点占用了哪个 GPIO bank 的哪个 pin、高/低有效
- **引脚复用（Pinctrl）**：各状态节点的 `pins` 与 `function`
- **时钟 / 频率**：所有 `clock-frequency`、`max-frequency` 等，自动换算 Hz→kHz/MHz/GHz
- **中断**：中断号、名称、父中断控制器

寄存器地址会依据继承的 `#address-cells` / `#size-cells` 正确解析，例如 `0x1C28000+0x400`。

---

## DTS 差异对比

Web 界面点 **「对比 DTS」**（CLI 用 `diff` 命令）可对比两份 DTS：

- 分别选择 A（基准）和 B（对比）文件，也可用当前编辑器内容作为任一侧，还能一键交换 A/B
- 差异以**红色高亮**显示：删除行加删除线、新增/修改行红色背景，**改动的词**用深红底精确标出
- 顶部显示新增 / 删除 / 修改 / 相同行数统计
- 勾选「只显示差异行」可过滤掉未变化的行

对比采用行级对齐 + 词级差异定位，只标出真正变化的 token（例如只标 `"v1"`→`"v2"`，不标整行），便于快速定位配置改动。

---

## 两种编译模式（Web 界面开关）

- **纯 DTS（默认）**：无预处理指令，直接编译。适合反编译产物或手写 DTS。
- **带 #include**：源码树风格 DTS，内置预处理器支持：
  - `#include`（内置常用 `dt-bindings` 头文件：`gpio.h`、`irq.h`、`arm-gic.h`、`input-event-codes.h`、`thermal.h`、`regulator.h`）
  - `#define`（对象宏 / 函数宏）、`#undef`
  - `#ifdef` / `#ifndef` / `#if` / `#elif` / `#else` / `#endif`
  - `/include/ "xxx.dtsi"`

> SoC 专有宏（如全志 / 瑞芯微的 clock、pinctrl 头文件）需在界面上传对应 `.h` / `.dtsi` 文件。若某个头文件实在缺失，日志会明确提示。
> 需要真正的 dtc 语义时，命令行加 `--engine dtc`（Linux 上 `apt install device-tree-compiler`）。

---

## 设计说明

- **零依赖**：仅用 Python 标准库，适合精简系统，也方便 Windows 直接运行。
- **安全写回**：任何覆盖原 DTB 的操作都先把编译产物写入临时文件并校验 FDT 头，通过后才备份原文件并写回，避免写坏文件。
- **内置 FDT 解析**：`info` 与编译产物校验不依赖 dtc，自带 FDT 头 / structure block 解析。
- **两遍 phandle 分配**：编译 DTS 时自动收集 label 与路径引用，为被引用的节点分配并注入 phandle。

---

## Web API

界面由内置 `http.server` 提供，主要接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 引擎与版本状态 |
| GET | `/api/dts_files` | 最近打开的 DTS 列表 |
| GET | `/api/info` | DTB 头信息 |
| GET | `/api/download?token=` | 下载编译产物 |
| PUT | `/api/upload` | 上传 DTB |
| PUT | `/api/upload_header` | 上传 `.h` / `.dtsi` 头文件 |
| POST | `/api/remember_dts` | 记住打开的 DTS |
| POST | `/api/compile_dts` | 编译 DTS → DTB 并生成下载链接 |
| POST | `/api/validate_dts` | 仅做语法检查 |
| POST | `/api/analyze` | 分析 DTS，返回结构化数据 + 文本报告 |
| POST | `/api/diff` | 对比两份 DTS，返回词级差异 + 统计 + 文本报告 |

> Web 服务默认监听本机 `127.0.0.1:8080`，仅建议在可信环境使用。

---

## 许可证

MIT License — 见 [LICENSE](LICENSE)。
