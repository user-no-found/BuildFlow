# BuildFlow

BuildFlow 是 Python 3.11+ 的原生构建执行器。项目声明命令和产物；BuildFlow 负责同一条步骤流水线、持续日志、真实退出状态、进程取消、项目锁与成功发布。Linux 使用原生进程组，Windows 使用原生 Job Object；构建不会启动容器。

## 首次初始化

在工具检出目录执行一次：

```sh
/path/to/BuildFlow/setup.sh
```

原生 Windows 使用：

```bat
C:\tools\BuildFlow\setup.cmd
```

初始化登记当前检出的 `BUILDFLOW_PATH`。Linux/Bash 在 `~/.bashrc` 添加一个有明确边界的加载区块，新交互 Bash 自动导出工具路径。应用的薄入口可以读取同一生成文件，因此现有终端无需手动 `export` 或 `source` 就能调用已接入的应用构建脚本。命令只写用户配置，不安装包，不修改系统或全局 `PYTHONPATH`。

配置放在 `${XDG_CONFIG_HOME:-$HOME/.config}/buildflow/`；`XDG_CONFIG_HOME` 如果设置必须是绝对路径。Windows 同样使用 `XDG_CONFIG_HOME` 或用户目录下 `.config`，并写入 `HKEY_CURRENT_USER\Environment` 的用户变量和广播环境变化。现有父 `cmd.exe` 的环境不会被子进程修改；重新打开终端可读取新变量。BuildFlow 自身的 `build.cmd` 能从所在目录直接运行，也可由接入 Python `resolve_tool` 的其他入口读取用户配置。

`environment.json` 是工具路径登记源，结构为 `{"schema": 1, "tools": {"BUILDFLOW_PATH": "/absolute/checkout"}}`；`environment.sh` 是同步生成、正确 shell 引用的派生文件。只通过 `setup.sh` / `setup.cmd` 更新，**不要手工编辑 JSON 或生成脚本**。工具移动后在新位置重新执行 setup。配置只允许 `BUILDFLOW_PATH` 和 `FNPACKKIT_PATH`，不登记应用、工作区或构建参数。

重复初始化相同路径不重复区块或备份；改动前备份原文件为同目录的 `<name>.buildflow-backup-<id>`，终端会打印备份位置。保留 Bash 配置的其他内容和文件权限，拒绝替换符号链接配置或破损的管理区块。每个文件单独原子写入，整个多文件操作不提供断电事务保证；异常退出后按错误修复文件权限或路径，再重跑 setup 使 JSON 和生成文件一致。需要恢复原配置时，用打印的备份替换对应文件；恢复路径登记时将 JSON 和 shell 备份作为同一次操作恢复。Windows 修改前另存注册表旧值备份。

显式环境优先于登记配置：已有变量即使为空也不会被生成脚本覆盖；显式空值或无效工具目录会报错。更新登记路径时，当前终端已有的旧环境变量也会保持优先，必要时 `unset BUILDFLOW_PATH` / `unset FNPACKKIT_PATH` 后再调用入口，或打开新的终端。

供其他工具的共享初始化接口：

```sh
python3 -m buildflow.bootstrap \
  --register BUILDFLOW_PATH /absolute/BuildFlow \
  --register FNPACKKIT_PATH /absolute/FnPackKit
```

仅首次定位工具时，薄 POSIX 入口可以读取固定用户位置的受管理环境文件，再定位工具包：

```sh
buildflow_config=${XDG_CONFIG_HOME:-$HOME/.config}/buildflow/environment.sh
if [ -f "$buildflow_config" ]; then
    . "$buildflow_config"
fi
```

Python 调用方可使用 `buildflow.tool_environment.resolve_tool("BUILDFLOW_PATH")`：显式环境存在时先验证它，否则读取登记路径，均核验 `pyproject.toml` 身份和源码包；完全未配置时返回 `None`。`buildflow.bootstrap.register_tools` 接受工具变量到绝对路径的映射，`register_tool` 为单项入口；两者共用锁、备份和宿主环境接入。

## 运行

从本地检出运行，不需要安装 Python 运行时依赖：

```sh
/path/to/BuildFlow/build.sh --project /path/to/application/project.json
/path/to/BuildFlow/build.sh --workspace /path/to/workspace
/path/to/BuildFlow/build.sh --workspace /path/to/workspace --discover
```

原生 Windows：

```bat
C:\tools\BuildFlow\build.cmd --project C:\work\application\project.json
```

`PYTHON` 可指定 Python 可执行文件。根脚本从自身位置定位库，参数中的相对路径仍相对调用目录。库也可从明确的本地路径安装：

```sh
python3 -m pip install /path/to/BuildFlow
buildflow --project /path/to/application/project.json
```

不安装时，设置 `PYTHONPATH=/path/to/BuildFlow/src` 后可使用 `python3 -m buildflow` 或 Python API。工具的源仓库是 <https://github.com/user-no-found/BuildFlow>；依赖方声明所需版本并显式选择安装版本或本地路径。入口不会自动克隆、更新工具或推断相邻目录。

## 项目声明

应用自己的 `project.json` 是身份、构建配置和产物声明的唯一来源。其他工具可增加各自的配置区，BuildFlow 只解释 `buildflow` 和通用身份字段。

```json
{
  "app_id": "example-app",
  "display_name": "示例应用",
  "version": "1.2.3",
  "buildflow": {
    "schema": 1,
    "requires": ">=0.1,<0.2",
    "targets": ["linux-amd64"],
    "target_os": "linux",
    "target_arch": "amd64",
    "steps": [
      {
        "name": "编译",
        "argv": ["go", "build", "-o", "{work}/example", "."],
        "cwd": "{project}",
        "env": {"GOOS": "linux", "GOARCH": "amd64"}
      },
      {
        "name": "验证程序",
        "argv": ["{work}/example", "--version"]
      }
    ],
    "artifacts": [{"path": "{work}/example", "name": "example_{version}"}]
  }
}
```

- `schema` 必须为整数 `1`；`app_id` 和 `display_name` 必须是非空字符串。
- `requires` 默认 `>=0.1,<0.2`，接受逗号分隔的 `>=`、`<=`、`==`、`>`、`<` 数字版本约束。
- `targets` 是目标标签列表，可表示平台或包格式；`target_os`、`target_arch` 为可选的目标平台说明。它们用于选择及报告，实际工具链和交叉编译参数由命令显式声明。
- `steps` 按顺序执行，默认空列表。通用 CLI 拒绝空步骤构建并提示使用项目入口或对应格式工具；显式 adapter 也必须返回非空计划。库 API 允许格式工具提供自己的步骤。每一步要求 `name`、非空字符串数组 `argv`；`cwd` 默认 `{project}`，相对路径以项目根为基准。`env` 字符串映射覆盖继承环境。不经 shell 展开命令，不隐式激活工具链。
- `{project}` 是项目根，`{work}` 是本次独立工作区，`{output}` 是项目发布目录，`{python}` 是当前解释器，`{version}` 是本次写回的项目版本。仅替换这些精确 token；一般的 JSON、大括号或命令参数保持原样。
- `artifacts` 默认空列表，每项要求源 `path` 和直接输出文件名 `name`，不接受子目录。源必须是常规文件，输出名称不得越界。可执行权限被保留。

步骤将中间文件和待发布文件写入 `{work}`，由 `artifacts` 发布。`{output}` 可用于读取已有成功结果。任意声明命令和 Python adapter 具有当前用户权限，能够自行修改文件；BuildFlow 的发布规则适用于自己的文件操作，不是命令沙箱。

配置每次构建重新读取；修改声明无需重装工具。所需编译器、SDK 和签名工具必须在本机构建环境可用，BuildFlow 不替代语言工具链。项目可以编译自身代码，也可以通过来源 API 获取上游源码。

## 应用版本

可选顶层 `project.json.version` 是应用唯一版本源；没有该字段的通用项目保持无版本交互。已声明时，构建在项目锁内重新读取版本，显示已安装版本和项目版本，再选择正式或测试类型，最后选择本次版本。已安装信息由调用者提供，只用于展示；没有探测器时显示“未检测”，探测器确认不存在时显示“未安装”。

正式版本是严格 `x.x.x`，测试版本是 `x.x.x-xx`。数字部分无前导零，测试后缀为正整数、至少两位补零；例如 `-01`、`-99`、`-100`，不接受 `-00`、`-1`、`-001`。类型菜单回车保持项目当前类型，版本菜单回车使用下表默认值：

| 当前项目版本 | 选择类型 | 默认构建版本 |
|---|---|---|
| `1.2.3` | 正式 | `1.2.4` |
| `1.2.3-02` | 测试 | `1.2.3-03` |
| `1.2.3` | 测试 | `1.2.3-01` |
| `1.2.3-02` | 正式 | `1.2.3` |

默认值始终来自项目版本。手动输入必须与所选类型一致。选择通过后，先原子写回 `project.json.version`，再创建适配计划和执行构建；后续失败仍保留用户已选版本。非法输入、EOF 或选择期间取消不改版本。写回前重新核对 JSON 内容；若用户在选择期间修改了其他字段，拒绝覆盖并提示重新构建，保留外部编辑。锁覆盖版本读取、选择、写回、计划和发布，另一个并发构建不能读取旧值后覆盖选择。

非交互环境必须明确策略：

```sh
buildflow --project /path/to/project.json --build-version 1.2.4
buildflow --project /path/to/project.json --build-version 1.2.4-01
buildflow --project /path/to/project.json --next-version --channel test
```

`--build-version` 按格式推导类型；如果同时给 `--channel stable|test`，两者必须一致。`--next-version` 使用上述默认规则，省略 channel 时保持项目类型。EOF 从不当作回车默认。版本选择会改变应用项目文件，Git 用户可正常审查此配置变更。

## 工作区、日志和成功结果

一次构建写入以下项目私有目录：

```text
application/
  project.json
  .build/
    build.lock
    last-success.json
    <run-id>/
      build.log
      events.jsonl
      report.json
      reports/
  output/
    example_1.2.3
    example_1.2.4
```

`build.log` 在命令运行期间持续写入原始 stdout/stderr 字节及命令边界；两个流的相对顺序取决于操作系统调度。`events.jsonl` 记录结构化事件和完整解码输出。日志含原始 argv 和子进程数据，应按项目敏感信息管理，不能直接作为公开展示内容。

终端保留阶段标题和已完成动作，动态行显示当前语义动作、目标、实际耗时和安全命令摘要；本地 Python 操作期间也持续刷新。标题、状态、耗时分别着色，不估计百分比。`NO_COLOR` 只禁用颜色，`TERM=dumb` 禁用颜色和动画；stderr 重定向时输出稳定文本。UI 写 stderr，`>file` 只重定向 stdout，合并日志可用 `>file 2>&1`。中文按终端列宽裁剪，完成行保留耗时，错误和产物路径按列宽换行。不能编码 Unicode 符号时使用 ASCII 图标。Windows 控制台只有成功开启 VT 后才使用 ANSI，否则使用稳定文本；同一实现由 `build.sh` / `build.cmd` 或 Python API 复用。

应用或格式工具只报告动作语义，不输出 ANSI，也不依赖特定构建格式：

```python
def compile_target(ctx):
    with ctx.activity("编译程序", target="linux/amd64"):
        ctx.status("检查本地输入")
        check_inputs()
        ctx.runner.run(["go", "build", "-o", str(ctx.work / "app"), "."],
                       cwd=ctx.project.root)
```

`ctx.activity(name, *, target="")` 是可嵌套上下文，完成时保留动作结果，抛异常时只标记该动作结束；`ctx.status(message)` 更新当前动作详情。标签和详情必须是调用者有意提供的安全文本，不能包含令牌、原始 argv 或捕获的 JSON/补丁。原 `run` / `capture` 调用接口不变。Context 直接构造时动作接口默认不输出，不要求第三方 runner 实现额外属性。作用域使用 Python contextvars；将工作交给新线程时，可用 `contextvars.copy_context().run` 显式传递当前作用域。


独立提示使用 `ctx.notice(message, level="info")`，level 可选 info、warning、error。
提示通过同一事件线程输出完整文本，按终端宽度换行；TTY 分别使用青、黄、红色，
`NO_COLOR` 和稳定文本模式遵循相同的显示规则。

需要同步终端输入的操作使用 `with ctx.interaction():`。进入时等待前序提示输出完成，
清除进度行并暂停心跳展示，其他展示延至作用域结束；退出或异常时恢复。
交互程序直接读写控制终端，输入不进入 BuildFlow 的命令日志或事件。
作用域内不要调用捕获输入的 runner，多个并行任务需要由调用方串行安排终端交互。

POSIX 步骤在实际需要 root 权限时调用共享授权方法：

```python
from buildflow.privilege import require_sudo

def verify_privileged(ctx):
    require_sudo(ctx, "隔离生命周期验证")
    run_bounded_root_fixture()
```

`require_sudo` 复用有效的系统授权；需要密码时先输出黄色提示，再由系统 `sudo -v`
直接在控制终端请求授权。未授权的非交互环境、缺少 sudo、检查超时或授权失败会明确停止。
它只取得授权，具体 root 命令仍由调用方明确执行；不修改 sudo 策略或使整个构建以 root 运行。

事件中的 `activity_start/end` 使用同一 `id`，开始事件含 `parent_id/name/target`，结束事件含 `outcome/elapsed`；`activity_status` 含 `activity_id/message`。`command` 和 `command_end` 使用同一 `id/activity_id/label`，结束事件含真实 `returncode/elapsed`，未启动的进程返回码为 `null`。命令摘要跳过已识别的 `nice` / `taskset` 包装，仅显示安全的主程序名和允许的 Go 子命令，不展示其余参数或环境。TTY 不将任意子进程输出转成活动详情；`capture` stdout 只返回调用者并保留在日志和事件中，不回显。非 TTY 的普通命令输出保留可读文本，跨输出块清理终端控制序列。

非零命令退出并不自动意味着构建失败：应用可以捕获预期负向验证的异常。只有逃出构建操作的异常决定最终失败。失败摘要包含阶段、退出码和有界输出尾部，stderr/stdout 各最多 6 行、每行最多末尾 400 个字符；完整诊断仍在日志中。`BuildError.reported` 表示最终错误事件已经交付 UI，外层 CLI 只在它为 false 时打印异常，避免重复摘要。

每次 API 构建使用一个事件写入线程负责日志与 UI，并提供固定频率心跳；`ui(event)` 回调及可选 `ui.close()` 都在该线程调用。结束时排空队列、停止心跳并恢复终端状态。发布前检查事件记录和 UI 故障，故障会阻止发布；发布完成后的显示故障只报告“产物已发布”警告，不将已发布结果伪报为构建失败。底层命令失败时，显示故障不覆盖其真实退出码。

全部步骤成功且所有产物完成复制、SHA-256 和权限检查后，文件直接发布到 `output/<声明文件名>`。不同版本并存；同名文件仅在内容与权限完全相同时复用，否则明确拒绝覆盖，要求选择新版本。报告保留于 `.build/<run-id>/report.json`，记录应用版本、工具输入、主机和目标、metadata、每个产物的体积、哈希、权限和日志位置。应用报告、截图和内部证据也应写在 work 内，不列入交付 Artifact。

每个文件通过同一文件系统的硬链接原子创建，不覆盖已有文件；所有文件完成后才原子替换 `.build/last-success.json`。该指针 schema 为 `2`，包含 `run_id`、相对 `.build` 的 `report` 路径，以及 `artifacts` 文件清单（`name/size/sha256/mode`）。常规错误或取消会回滚本次新添加的文件，复用的旧文件保持不变，上一成功指针保持不变。**多个文件不是一组原子事务**；强制结束进程或断电可能留下尚未被成功指针引用的新文件。输出文件系统必须支持硬链接，不退回到会暴露半文件的直接复制。

已有 `output/runs` 和旧指针不被删除，新发布不写入也不读取它们。消费方读取 `.build/last-success.json`，从其报告定位 `output/` 文件；没有旧布局兼容读取。
项目写锁由系统持有，进程退出后自动释放；锁文件本身持续存在。并发构建退出码为 `73`，取消为 `130`，子进程失败保留其退出码；POSIX 信号退出按 `128 + signal` 转换。项目 `.build`、`output` 和内部发布目录不能是符号链接。

## 只读发现

`--project` 直接读取指定声明，接受项目目录或 JSON 路径。在应用根目录运行且未指定范围时使用当前 `project.json`。其他情况要求明确的 `--workspace`，可重复传多个路径。不会扫描 home、保存项目登记、记住上次选择或执行 adapter。

发现默认最多深度 `4`、目录 `1000`，可用 `--max-depth` 和 `--max-directories` 修改。跳过 `.git`、`.cache`、`.build`、`output`、`dist`、`build`、`target`、`vendor`、`node_modules`、`venv`、`.venv`、`__pycache__` 和符号链接。路径去重、稳定排序；不含 `buildflow` 的普通 JSON 不作为应用。声明错误、版本不匹配、缺少通用步骤而需专用入口、重复 ID、权限或预算限制会明确显示。重复 ID 仍以完整路径区分，不按 ID 自动选择。

`--discover` 输出 JSON 且绝不导入 adapter；扫描不完整时退出 `2`。选择界面要求终端输入和终端错误输出；非交互环境必须用 `--project`。通过 `--adapter module:function` 显式提供步骤时，仅选定项目并开始构建后才导入模块；入口函数接受 `Project` 并返回 `list[Step]`。适配代码需在正常 Python 模块搜索路径上。

## 库接口

```python
from pathlib import Path
from buildflow.api import build, declared_steps
from buildflow.contracts import Artifact, Step


def compile_project(ctx):
    ctx.runner.run(["go", "build", "-o", str(ctx.work / "program"), "."],
                   cwd=ctx.project.root)
    ctx.metadata["compiler"] = ctx.runner.capture(["go", "version"], cwd=ctx.work).strip()

result = build(
    Path("/path/to/application/project.json"),
    steps=[Step("原生编译", compile_project)],
    artifacts=lambda ctx: [Artifact(ctx.work / "program", "program")],
    metadata={"purpose": "native build"},
)
print(result.report)
```

`build(project_path, *, steps=None, artifacts=None, metadata=None, ui=None, build_version=None, next_version=False, channel=None, installed_version=None, configure=None)` 返回 `BuildResult(run_id, output, report, metadata)`；output 是项目 `output/`，report 是本次 `.build/<run-id>/report.json`。`steps=None` 使用声明步骤；显式列表替代声明。`declared_steps(project)` 可把已验证声明转为步骤，方便其他包追加自己的步骤。`artifacts` 接受列表或构建结束后调用的 `callable(ctx)`；省略时使用声明产物。

`installed_version` 接受显示字符串或 `callable(project)`，回调返回 `None` 表示未安装；省略参数表示未检测。`configure(project)` 在持锁、版本写回及重载之后调用，返回包含 `steps/artifacts/metadata` 的字典；依赖项目元数据的 adapter 必须在此回调中创建，以免闭包捕获选择前的版本。`ctx.project` 与 configure 接收同一份本次配置。

每个 `Step(name, action)` 的 `action(ctx)` 接收 `Context`：`project`（`path/root/data/config`）、`work`、`output`、`runner`、可写 `metadata`。步骤直接抛出异常即失败。`BuildError(message, exit_code=1)` 保留可报告退出码。`Runner(log_path, sink=None)` 可独立使用；`run(argv, cwd, env=None)` 返回 `Completed`，`capture` 只返回 stdout 字符串，同时仍把两流落盘。runner 不把失败的输出冒充成功结果。

`Terminal(stream=None)` 是独立 callable sink，接受 `build_start`、`step_start`、`output`、`tick`、`step_end`、`build_end`、`error` 事件，不启动进程。各事件字段可从 `contracts`、`pipeline` 和 `api` 的调用查看；自定义 sink 可通过 `build(ui=sink)` 接入，原始日志仍由引擎保留。

`load_project(path, version="")` 从 `buildflow.project` 导入；传工具版本时验证约束。`select_project(project=None, workspaces=(), target=None, max_depth=4, max_directories=1000, validator=None)` 从 `buildflow.cli` 导入，返回 `Project`。其他格式工具可提供 `validator(path)->Project`，在发现时就显示格式专属错误。`buildflow.discovery.discover` 只返回候选与警告，不做选择。

## PNG 与 ICO 资产

图像功能使用可选依赖 `buildflow[images]`（Pillow 12.3.0）；普通构建与 `buildflow.images` 导入不会初始化 Pillow。源码方式运行时，由调用方环境提供该依赖，未安装时调用图像功能会给出明确错误。

```python
from buildflow.images import inspect_png, prepare_icons

info = inspect_png(ctx.project.root / "assets/icon.png")
icons = prepare_icons(
    ctx.project.root / "assets/icon.png", ctx.work,
    png_sizes=(64, 256),
    variants={64: ctx.project.root / "assets/icon-64.png"},
    ico_sizes=(16, 32, 64),
)
ctx.metadata["icons"] = icons["metadata"]
# icons["png"]: {64: Path(...), 256: Path(...)}
# icons["ico"]: Path(...)；未请求 ICO 时为 None
```

`inspect_png(path, *, max_bytes=20971520, max_pixels=16777216)` 返回 `path/width/height/mode/bytes/sha256/color_space/color_basis`。读取普通单帧 PNG，检查完整结构、CRC、像素与字节上限；拒绝动画、截断、额外尾部、符号链接及含 `..` 的路径。不接受 JPEG 或仅改扩展名的文件。

无色彩标记的 PNG 按 sRGB 解释，并记录 `assumed-untagged`，而非声称有显式色标。支持标准 sRGB/gAMA/cHRM 组合；矛盾声明拒绝。ICC 是否可保留通过与 LittleCMS 标准 sRGB profile 的颜色变换标签及相关头字段比较，不以名称猜测：匹配的 profile 标为 `srgb/icc-srgb-transform`；其他有效 RGB/灰度 ICC 标为 `icc/icc-profile`，可以作为转换主源。不同形式但无法确认的 sRGB ICC 也可能落入此类，不能作为要求原字节保留的显式变体。非标准且没有 ICC 的 gamma/色度返回 `unsupported`，生成接口拒绝；HDR PNG 不支持。

`prepare_icons(source, work, *, png_sizes=(64, 256), variants=None, ico_sizes=())` 为每次调用创建独立 `work/assets/icons-*` 目录，不回写源文件，不覆盖既有资产；调用方应提供自己管理的工作目录。`png` 映射使用整数尺寸键，`metadata` 可直接序列化为 JSON，记录源文件、显式变体、输出的路径/摘要/大小，以及缩放和透明补边规则。异常或取消清理本次输出目录。

显式 `variants` 必须对应请求的 PNG 尺寸、精确正方形且具有支持的 sRGB 色彩解释；合格时复制原字节，包括 RGB/调色板等原始模式。非法变体明确拒绝，不用主源替换。缺失尺寸由主源等比缩放并居中透明补边，输出带 sRGB 标记的 8 位 RGBA PNG；需要转换的 ICC 主源通过色彩管理转换并保留 alpha。ICO 按请求尺寸独立派生，不借用显式 PNG 变体。

PNG 尺寸范围为 1..4096，ICO 为 1..256，各最多 32 个不重复整数尺寸；合计输出最多 16777216 像素，合计输入最多 41943040 字节，每个输入另受默认 PNG 限制。工具不内置应用名称、品牌配置、FPK 路径或飞牛的文件大小规则，这些由消费方检查和映射。

## 固定 Git 来源

```python
from buildflow.source import resolve_git, fetch_git


def fetch_source(ctx):
    source = resolve_git("https://example.org/team/source.git", runner=ctx.runner)
    checkout = fetch_git(source, ctx.work / "source",
                         ctx.project.root / ".cache" / "git", runner=ctx.runner)
    ctx.metadata["source"] = {"url": source.url, "revision": source.revision, "ref": source.ref}
```

默认 `ref="HEAD"`，每次新构建显式解析一次上游默认分支 HEAD，后续下载、构建和验证使用同一个完整 SHA。网络解析失败就失败，绝不将旧缓存称作最新。指定完整 SHA 或构造 `ResolvedSource(url, revision, ref)` 可重现已记录输入；对象缓存仅按 URL 和 SHA 使用，缺失对象仍需取回。分支名或完整 ref 必须唯一匹配。`fetch_git` 拒绝已存在的目标检出，不修改用户源码仓库，缓存锁避免并发写入。

## 验证

```sh
python3 -m unittest discover -s tests -v
# 仅验证初始化（测试自动隔离 HOME 和 XDG）
python3 -m unittest discover -s tests -p test_setup_e2e.py -v
BUILDFLOW_TEST_ARTIFACTS=/absolute/evidence python3 -m unittest discover -s tests -v
```

测试使用临时项目、真实子进程和本地 Git 仓库；显式指定证据目录时保留项目、日志、产物及测试环境描述。POSIX 信号测试只在 POSIX 运行；Windows 的 `.cmd`、文件锁和 Job Object 需要原生 Windows 环境验证。
