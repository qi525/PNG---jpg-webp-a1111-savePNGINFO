# =================================================================
# 步骤 1：安装必要的依赖库和打包工具
# 请确保您的环境中已经安装了 Python (建议使用 Python 3.8 或更高版本)。
# 确保在运行这些命令时，您的命令行窗口处于正确的 Python 虚拟环境（如果使用）。
# =================================================================

installation_commands = """
# 1. 安装代码中使用的所有第三方库：
pip install pandas pillow tqdm loguru piexif openpyxl

# 2. 安装 Python 打包工具 PyInstaller：
pip install pyinstaller
"""

print(f"请在您的命令行中执行以下安装命令：\n{installation_commands}")


# =================================================================
# 步骤 2：使用 PyInstaller 打包生成 EXE 文件
# 确保 image_processor_and_converter.py 文件位于当前命令行目录下。
# =================================================================

# 命令行执行打包指令
pyinstaller_command = (
    "pyinstaller "
    # 打包成一个独立的 EXE 文件
    "--onefile "
    # 打包后运行时保留命令行窗口（因为代码需要用户输入和输出日志）
    "--console "
    # 设置生成的 EXE 文件名为 "SD_Image_Converter"
    "--name SD_Image_Converter "
    # 强制包含 openpyxl 库，以防 pandas 写入 Excel 时找不到
    "--hidden-import=openpyxl "
    # 指定要打包的脚本文件
    "image_processor_and_converter.py"
)

print("\n=================================================================")
print("步骤 2：执行打包命令")
print("=================================================================")
print(f"请在命令行中（确保在脚本文件目录下）执行以下命令：")
print(f"{pyinstaller_command}")


# =================================================================
# 步骤 3：查看结果
# =================================================================

# 打包成功后，EXE 文件将在生成的 'dist' 文件夹中
result_info = """
打包过程可能需要几分钟时间，请耐心等待。
如果打包成功，您将在脚本所在目录下找到一个名为 'dist' 的文件夹。

生成的 EXE 文件路径为：
./dist/SD_Image_Converter.exe

您可以直接双击此文件来运行您的图片转换和元数据校验工具。
"""

print(f"\n{result_info}")