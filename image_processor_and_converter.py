# image_processor_and_converter.py

# -*- coding: utf-8 -*-
import os
import re
import sys
import warnings 
import pandas as pd
import concurrent.futures 
from PIL import Image, ImageFile, ExifTags
from datetime import datetime
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE # 导入用于清理非法字符的正则
from typing import List, Dict, Any 
from tqdm import tqdm 
from loguru import logger 

# 允许 Pillow 加载截断的图像文件，避免程序崩溃。
ImageFile.LOAD_TRUNCATED_IMAGES = True

# 全局变量，用于在警告处理函数中访问当前处理的文件路径
_current_processing_file = None

# 定义最大并发进程数 (通常是CPU核心数)
MAX_WORKERS = os.cpu_count() or 4

# 配置 Loguru (符合用户对日志的要求)
# 日志文件记录 ERROR 级别的信息
logger.add("image_processor_error.log", rotation="10 MB", level="ERROR", encoding="utf-8")
# 默认的控制台输出级别设置为 INFO
logger.configure(handlers=[
    {"sink": sys.stdout, "level": "INFO"}
])


# --- 正向提示词的停用词列表 (用于提取核心词) ---
POSITIVE_PROMPT_STOP_WORDS = [
    # ----------------------------------------------------
    # 核心词汇，一行算一个部分
    # (已根据用户要求，将每行视为一个整体词组)
    # ----------------------------------------------------
    # 第一行
    r"newest, 2025, toosaka_asagi, novel_illustration, torino_aqua, izumi_tsubasu, oyuwari, pottsness, yunsang, hito_komoru, akeyama_kitsune, fi-san, rourou_\(been\), gweda, fuzichoco, shanguier, anmi, missile228, ",
    "2025, toosaka_asagi, novel_illustration, torino_aqua, izumi_tsubasu, oyuwari, pottsness, ",
    "looking_at_viewer, curvy,seductive_smile,glamor,makeup,blush,, lace,ribbon,jewelry,necklace,drop earrings,pendant,, sexually suggestive,",
    # ----------------------------------------------------
    # 第二行
    "sexy and cute,",
    # ----------------------------------------------------
    # 第三行
    "dynamic pose, sexy pose,",
    # ----------------------------------------------------
    # 第四行 (包含质量标签和角度词)
    r"dynamic angle,, dutch_angle, tinker bell \(pixiv 10956015\),, masterpiece, best quality, amazing quality, very awa,absurdres,newest,very aesthetic,depth of field,",
    "very awa,absurdres,newest,very aesthetic,depth of field,",
]
# ------------------------------------------------------


def custom_warning_formatter(message, category, filename, lineno, file=None, line=None):
    """
    自定义警告格式化器，尝试获取当前处理的文件路径。
    """
    global _current_processing_file
    
    # 检查警告是否来自 PIL 的 TiffImagePlugin 并且是 Truncated File Read
    if category is UserWarning and "Truncated File Read" in str(message) and "TiffImagePlugin.py" in filename:
        if _current_processing_file:
            return f"UserWarning: {message} for file: '{_current_processing_file}'\n"
    
    # 对于其他警告，使用默认格式
    return warnings.formatwarning(message, category, filename, lineno, line)

# 设置自定义警告格式化器
warnings.formatwarning = custom_warning_formatter


def process_single_image(absolute_path: str) -> Dict[str, Any] | None:
    """
    处理单个图片文件，提取元数据并返回结构化数据。
    (来自 image_scanner.py)
    """
    global _current_processing_file # 声明使用全局变量

    image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
    
    # 确保文件存在且是图片扩展名
    if not os.path.exists(absolute_path) or not absolute_path.lower().endswith(image_extensions):
        return None # 不是图片或文件不存在，返回None
    
    # 定义一个更通用的正则表达式，用于从原始文本中捕获 Stable Diffusion 的信息块
    sd_full_info_pattern = re.compile(
        r'.*?(?:masterpiece|score_\d|1girl|BREAK|Negative prompt:|Steps:).*?(?:Version:.*?|Module:.*?|)$',
        re.DOTALL # 允许.匹配换行符
    )
    # 定义一个更严格的正则，用于最终验证是否是有效的SD参数
    sd_validation_pattern = re.compile(r'Steps: \d+, Sampler: [\w\s]+', re.DOTALL)
    
    # 初始化变量
    containing_folder_absolute_path = os.path.abspath(os.path.dirname(absolute_path))
    sd_info = "没有扫描到生成信息"
    sd_info_no_newlines = "没有扫描到生成信息"
    positive_prompt = ""
    negative_prompt = ""
    other_settings = ""
    model_name = "未找到模型"
    positive_prompt_word_count = 0
    raw_metadata_string = ""
    creation_date_dir = "未获取日期"
    core_positive_prompt = "核心词为空" 

    _current_processing_file = absolute_path # 在处理每个文件前更新全局变量

    try:
        # --- 获取文件创建日期 ---
        try:
            creation_time = os.path.getctime(absolute_path)
            dt_object = datetime.fromtimestamp(creation_time)
            creation_date_dir = dt_object.strftime("%Y-%m-%d")
        except Exception:
            pass 
        
        # --- 开始图像元数据提取 ---
        with Image.open(absolute_path) as img:
            # --- 阶段 1: 尝试从标准位置获取原始元数据字符串 ---
            if "png" in img.format.lower() and "parameters" in img.info:
                raw_metadata_string = img.info["parameters"]
            elif "jpeg" in img.format.lower() or "webp" in img.format.lower():
                if hasattr(img, '_getexif'):
                    exif_data = img._getexif()
                    if exif_data:
                        for tag, value in exif_data.items():
                            if tag in [0x9286, 0x010E]: # UserComment (0x9286) or ImageDescription (0x010E)
                                try:
                                    if isinstance(value, bytes):
                                        raw_metadata_string = value.decode('utf-8', errors='ignore')
                                        if not re.search(r'Steps:', raw_metadata_string):
                                            raw_metadata_string = value.decode('latin-1', errors='ignore')
                                    elif isinstance(value, str):
                                        raw_metadata_string = value
                                    break
                                except Exception:
                                    pass
            
            # --- 阶段 2: 清理并使用更强大的正则表达式提取有效信息 ---
            if isinstance(raw_metadata_string, str) and raw_metadata_string:
                # 移除 Excel 不支持的非法 XML 字符
                cleaned_string = ILLEGAL_CHARACTERS_RE.sub(r'', raw_metadata_string)
                
                # Clean up the "UNICODE" prefix
                if cleaned_string.startswith("UNICODE"):
                    cleaned_string = cleaned_string[len("UNICODE"):].lstrip() 
                
                # 尝试使用新的正则表达式捕获核心SD信息块
                match = sd_full_info_pattern.search(cleaned_string)
                
                if match:
                    extracted_text = match.group(0).strip() 
                    # 再次使用更严格的正则验证，确保提取的是有效的SD参数
                    if sd_validation_pattern.search(extracted_text):
                        sd_info = extracted_text
                        sd_info_no_newlines = sd_info.replace('\n', ' ').replace('\r', ' ').strip()
                        
                        # --- 阶段 3: 切割信息 ---
                        # 从后往前切割 '其他设置'
                        other_settings_match = re.search(r'(Steps:.*)', sd_info_no_newlines, re.DOTALL)
                        if other_settings_match:
                            other_settings = other_settings_match.group(1).strip()
                            temp_sd_info = sd_info_no_newlines[:other_settings_match.start()].strip()
                        else:
                            temp_sd_info = sd_info_no_newlines.strip()

                        # 切割 '负面提示词' 和 '正面提示词'
                        negative_prompt_match = re.search(r'(Negative prompt:.*?)(?=\s*Steps:|$)', temp_sd_info, re.DOTALL)
                        if negative_prompt_match:
                            negative_prompt = negative_prompt_match.group(1).replace("Negative prompt:", "").strip()
                            positive_prompt = temp_sd_info[:negative_prompt_match.start()].strip()
                        else:
                            positive_prompt = temp_sd_info.strip()
                        
                        # 统计正面提示词字数
                        positive_prompt_word_count = len(positive_prompt)

                    else:
                        sd_info = "没有扫描到生成信息"
                        sd_info_no_newlines = "没有扫描到生成信息"
                else:
                    sd_info = "没有扫描到生成信息"
                    sd_info_no_newlines = "没有扫描到生成信息"

            # --- 阶段 4: 提取正向提示词的核心词 ---
            core_positive_prompt = positive_prompt
            # 将所有停用词替换为空字符串
            for word in POSITIVE_PROMPT_STOP_WORDS:
                core_positive_prompt = f" {core_positive_prompt} "
                
                core_positive_prompt = re.sub(
                    re.escape(word), 
                    " ",             
                    core_positive_prompt,
                    flags=re.IGNORECASE 
                )

            # 3. 清理结果：移除多余的空格和首尾空格
            core_positive_prompt = core_positive_prompt.strip()
            core_positive_prompt = re.sub(r'\s+', ' ', core_positive_prompt)
            
            if not core_positive_prompt:
                core_positive_prompt = "核心词为空"
                
            # 从 other_settings 中提取 Model 信息
            model_match = re.search(r'Model: ([^,]+)', other_settings)
            if model_match:
                model_name = model_match.group(1).strip()


    except Exception as e:
        logger.error(f"Error processing image file '{absolute_path}' : {e}") 
        # 发生任何错误时都保持默认值
    finally:
        _current_processing_file = None 

    # 返回结果字典
    return {
        "所在文件夹": containing_folder_absolute_path,
        "图片的绝对路径": absolute_path,
        "图片超链接": f'={absolute_path}',
        "stable diffusion的 ai图片的生成信息": sd_info,
        "去掉换行符的生成信息": sd_info_no_newlines, 
        "正面提示词": positive_prompt,
        "负面提示词": negative_prompt,
        "其他设置": other_settings,
        "正面提示词字数": positive_prompt_word_count, 
        "模型": model_name, 
        "创建日期目录": creation_date_dir, 
        "提取正向词的核心词": core_positive_prompt 
    }


def get_png_files(folder_path: str) -> List[str]:
    """
    扫描指定文件夹及其子文件夹，收集所有 PNG 文件的绝对路径。
    """
    png_files = []
    for root, dirs, files in os.walk(folder_path):
        # 排除名为 '.bf' 的文件夹
        if '.bf' in dirs:
            logger.warning(f"发现并跳过文件夹: {os.path.join(root, '.bf')}")
            dirs.remove('.bf')
            
        for file in files:
            if file.lower().endswith('.png'):
                png_files.append(os.path.abspath(os.path.join(root, file)))
    return png_files

def extract_metadata_from_png(file_path: str) -> str:
    """
    从 PNG 文件中提取原始 'parameters' 元数据字符串。
    如果提取失败，返回空字符串。
    """
    try:
        with Image.open(file_path) as img:
            if "png" in img.format.lower() and "parameters" in img.info:
                return img.info["parameters"]
            return ""
    except Exception as e:
        logger.error(f"从 PNG 文件 '{file_path}' 提取元数据失败: {e}")
        return ""

def convert_and_write_metadata(
    png_path: str, 
    raw_metadata: str, 
    output_format: str, 
    output_dir_base: str
) -> str | None:
    """
    将 PNG 转换为目标格式，并将元数据写入新文件。
    (来自 image_converter.py)
    """
    
    # 1. 构建新的输出路径
    folder = os.path.dirname(png_path)
    output_sub_dir = os.path.join(folder, output_dir_base) 
    os.makedirs(output_sub_dir, exist_ok=True)
    
    base_name = os.path.splitext(os.path.basename(png_path))[0]
    new_file_name = f"{base_name}.{output_format}"
    output_path = os.path.join(output_sub_dir, new_file_name)
    
    try:
        # 2. 读取图像
        with Image.open(png_path) as img:
            
            save_kwargs = {}
            if raw_metadata:
                # 准备写入元数据到 EXIF UserComment (0x9286) 标签
                try:
                    # 创建一个简单的 Exif 字典 {tag_id: value}
                    # 标签 0x9286 (UserComment)
                    exif_data = {
                        0x9286: raw_metadata.encode('utf-8')
                    }
                    # 构建 ExifBytes 对象
                    exif_bytes = ExifTags.dump(exif_data)
                    save_kwargs['exif'] = exif_bytes

                except Exception as e:
                    logger.warning(f"为 '{output_path}' 准备 EXIF 元数据失败，将尝试不带 EXIF 写入: {e}")
            
            # 4. 保存图像
            if output_format == 'jpg':
                # 对于 JPG, 确保图片是 RGB 模式
                if img.mode == 'RGBA':
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3]) 
                    img = background
                elif img.mode != 'RGB':
                     img = img.convert('RGB')
                     
                img.save(output_path, 'jpeg', quality=95, **save_kwargs)
                
            elif output_format == 'webp':
                # WebP 保存
                img.save(output_path, 'webp', quality=95, **save_kwargs)
            else:
                logger.error(f"不支持的输出格式: {output_format}")
                return None
            
            return output_path
            
    except Exception as e:
        logger.error(f"转换或保存文件 '{png_path}' 到 '{output_path}' 失败: {e}")
        return None

def main_conversion_process(root_folder: str, choice: int):
    """
    主处理流程，包括扫描、转换、生成报告。
    (来自 image_converter.py)
    """
    
    # 1. 预处理
    output_format = 'jpg' if choice == 1 else 'webp'
    output_dir_base = f"png转{output_format.upper()}"
    report_file = f"png_conversion_report_{output_format}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    
    png_files = get_png_files(root_folder)
    total_files = len(png_files)
    
    if not total_files:
        logger.info(f"在 '{root_folder}' 中未找到任何 PNG 文件。")
        return
    
    logger.info(f"在 '{root_folder}' 中发现 {total_files} 个 PNG 文件。将转换为 {output_format.upper()}。")
    
    conversion_results = []
    success_count = 0
    failure_count = 0
    
    # 2. 转换和记录
    # 使用 tqdm 进行进度条计数
    for png_path in tqdm(png_files, desc=f"转换到 {output_format.upper()} 进度"):
        
        # 2.1 提取原始 PNG 元数据
        raw_png_info = extract_metadata_from_png(png_path)
        
        # 2.2 执行转换和写入元数据
        new_file_path = convert_and_write_metadata(
            png_path, 
            raw_png_info, 
            output_format, 
            output_dir_base
        )
        
        if new_file_path:
            # 2.3 扫描新文件的元数据进行对比
            # 注意: 这里使用单进程函数 process_single_image 来扫描新文件
            new_file_scan_result = process_single_image(new_file_path)
            
            new_file_info_string = (
                new_file_scan_result.get("去掉换行符的生成信息", "") 
                if new_file_scan_result else "未扫描到信息"
            )
            
            raw_png_info_no_newlines = raw_png_info.replace('\n', ' ').replace('\r', ' ').strip()
            
            # 2.4 对比结果
            is_consistent = "否"
            # 只要新文件的元数据包含在原始元数据中（或完全一致），就认为一致
            if raw_png_info_no_newlines and raw_png_info_no_newlines in new_file_info_string:
                is_consistent = "是"
            elif raw_png_info_no_newlines == new_file_info_string:
                 is_consistent = "是"
            
            
            # 2.5 记录结果
            conversion_results.append({
                "原文件的绝对路径": png_path,
                "原文件的pnginfo信息": raw_png_info_no_newlines,
                f"生成的{output_format.upper()}文件的绝对路径": new_file_path,
                f"生成的{output_format.upper()}文件的pnginfo信息": new_file_info_string,
                "原文件和生成文件的pnginfo信息是否一致": is_consistent,
            })
            success_count += 1
        else:
            failure_count += 1
            conversion_results.append({
                "原文件的绝对路径": png_path,
                "原文件的pnginfo信息": raw_png_info.replace('\n', ' ').replace('\r', ' ').strip(),
                f"生成的{output_format.upper()}文件的绝对路径": "转换失败",
                f"生成的{output_format.upper()}文件的pnginfo信息": "转换失败",
                "原文件和生成文件的pnginfo信息是否一致": "否 (转换失败)",
            })

    # 3. 结果总结和 Excel 报告生成
    logger.info("\n--- 转换总结 ---")
    logger.info(f"总数量: {total_files}, 成功: {success_count}, 失败: {failure_count}")

    if conversion_results:
        try:
            df = pd.DataFrame(conversion_results)
            df.to_excel(report_file, index=False, engine='openpyxl')
            report_abs_path = os.path.abspath(report_file)
            logger.info(f"报告已成功生成: {report_abs_path}")
            # 4. 自动运行打开 Excel 报告
            os.startfile(report_abs_path) 
        except Exception as e:
            logger.error(f"生成 Excel 报告失败: {e}")


if __name__ == "__main__":
    
    logger.info("--- PNG 图片批量转换和元数据校验工具启动 ---")
    
    # 1. 收集输入 - 文件夹路径
    while True:
        folder_path_input = input("请输入要扫描的文件夹绝对路径: ").strip()
        if os.path.isdir(folder_path_input):
            root_folder = folder_path_input
            break
        else:
            print("路径无效或文件夹不存在，请重新输入。")

    # 2. 收集输入 - 转换格式
    while True:
        try:
            choice_input = input("请选择转换格式 (1: JPG, 2: WebP): ").strip()
            choice = int(choice_input)
            if choice in [1, 2]:
                break
            else:
                print("无效的选择，请输入 1 或 2。")
        except ValueError:
            print("输入无效，请输入数字 1 或 2。")

    # 3. 执行主流程
    main_conversion_process(root_folder, choice)
    
    logger.info("--- 任务完成 ---")