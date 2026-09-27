import sys
import subprocess
import re
import shutil
import json
import time
from pathlib import Path
from dataclasses import dataclass
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QGridLayout, QLabel, QLineEdit, 
                             QPushButton, QFileDialog, QSpinBox, QDoubleSpinBox, 
                             QColorDialog, QComboBox, QGroupBox, QTextEdit, 
                             QCheckBox, QFormLayout)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

@dataclass
class PhraseContext:
    index: int
    de_text: str
    ru_text: str
    
    @property
    def base_name(self) -> str:
        return f"{self.index:04d}"

class BuilderThread(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.work_dir = Path("./build_tmp")
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def run(self):
        try:
            phrases = self._parse_file(Path(self.config['input_file']))
            self.log_signal.emit(f"Распарсено фраз: {len(phrases)}")
            
            video_segments = []
            for phrase in phrases:
                expected_segment = self.work_dir / f"{phrase.base_name}_segment.mp4"
                
                # Умный кэш: пропускаем уже готовые видео-сегменты
                if expected_segment.exists() and expected_segment.stat().st_size > 0:
                    self.log_signal.emit(f"[{phrase.base_name}] Найден готовый сегмент, пропускаем рендер...")
                    video_segments.append(expected_segment)
                    continue

                self.log_signal.emit(f"[{phrase.base_name}] Синтез аудио...")
                ru_audio, de_audio = self._generate_audio(phrase)
                
                self.log_signal.emit(f"[{phrase.base_name}] Рендер кадра...")
                img_path = self._create_frame(phrase)
                
                self.log_signal.emit(f"[{phrase.base_name}] Сборка сегмента FFmpeg...")
                segment_path = self._build_segment(phrase, img_path, ru_audio, de_audio)
                video_segments.append(segment_path)
                
            self.log_signal.emit("Запуск финальной конкатенации (demuxer)...")
            output_path = Path(self.config.get('output_file', 'final_output.mp4'))
            self._concatenate_segments(video_segments, output_path)
            
            if self.config.get('cleanup_tmp', False):
                self.log_signal.emit("Очистка временных файлов (build_tmp)...")
                shutil.rmtree(self.work_dir, ignore_errors=True)
                
            self.finished_signal.emit(f"Сборка успешно завершена: {output_path.absolute()}")
        except Exception as e:
            self.error_signal.emit(str(e))

    def _parse_file(self, filepath: Path) -> list[PhraseContext]:
        content = filepath.read_text(encoding='utf-8-sig')
        pattern = re.compile(r'\[(.*?)\]\s*;\s*\[(.*?)\]')
        return [PhraseContext(i, m.group(1).strip(), m.group(2).strip()) 
                for i, m in enumerate(pattern.finditer(content))]

    def _format_rate(self, speed: float) -> str:
        percent = int(round((speed - 1.0) * 100))
        return f"{percent:+}%"

    def _generate_audio(self, phrase: PhraseContext):
        ru_path = self.work_dir / f"{phrase.base_name}_ru.mp3"
        de_path = self.work_dir / f"{phrase.base_name}_de.mp3"
        
        ru_rate = self._format_rate(self.config['ru']['speed'])
        de_rate = self._format_rate(self.config['de']['speed'])
        
        # Локальная функция для выполнения TTS с механизмом Retry
        def run_tts(voice, rate, text, output_path):
            cmd = ["edge-tts", "--voice", voice, f"--rate={rate}", "--text", text, "--write-media", str(output_path)]
            max_retries = 3
            
            for attempt in range(max_retries):
                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return # Успешный выход из цикла
                except subprocess.CalledProcessError:
                    if attempt < max_retries - 1:
                        time.sleep(3) # Пауза 3 секунды перед новой попыткой (обход Rate Limit)
                    else:
                        raise RuntimeError(f"Сетевая ошибка сервера Microsoft TTS после {max_retries} попыток.")

        run_tts(self.config['ru']['voice'], ru_rate, phrase.ru_text, ru_path)
        run_tts(self.config['de']['voice'], de_rate, phrase.de_text, de_path)
        
        return ru_path, de_path

    def _wrap_text_spaced(self, text: str, font: ImageFont.FreeTypeFont, max_width: int, spacing: int) -> list[str]:
        words = text.split()
        lines, current_line = [], []
        for word in words:
            test_line = current_line + [word]
            test_str = ' '.join(test_line)
            w = sum(font.getlength(c) for c in test_str) + max(0, len(test_str)-1) * spacing
            if w <= max_width:
                current_line.append(word)
            else:
                if current_line: lines.append(' '.join(current_line))
                current_line = [word]
        if current_line: lines.append(' '.join(current_line))
        return lines

    def _draw_shadow_layer(self, shadow_draw, center_x, start_y, lines, font, shadow_cfg, spacing):
        if shadow_cfg['color'][3] == 0: return
        
        bbox = font.getbbox("Ay")
        line_height = bbox[3] - bbox[1] + 15
        y_cursor = start_y
        
        for line in lines:
            line_w = sum(font.getlength(c) for c in line) + max(0, len(line)-1) * spacing
            x_cursor = center_x - (line_w / 2) + shadow_cfg['x']
            sy = y_cursor + shadow_cfg['y']
            
            for c in line:
                shadow_draw.text((x_cursor, sy), c, font=font, fill=shadow_cfg['color'],
                                 stroke_width=shadow_cfg['size'], stroke_fill=shadow_cfg['color'])
                x_cursor += font.getlength(c) + spacing
            y_cursor += line_height

    def _draw_text_two_pass(self, text_draw, center_x, start_y, lines, font, cfg, spacing):
        bbox = font.getbbox("Ay")
        line_height = bbox[3] - bbox[1] + 15
        total_stroke = cfg['weight'] + cfg['outline_w']

        if total_stroke > 0:
            y_cursor = start_y
            for line in lines:
                line_w = sum(font.getlength(c) for c in line) + max(0, len(line)-1) * spacing
                x_cursor = center_x - (line_w / 2)
                for c in line:
                    text_draw.text((x_cursor, y_cursor), c, font=font, fill=cfg['outline_color'],
                                   stroke_width=total_stroke, stroke_fill=cfg['outline_color'])
                    x_cursor += font.getlength(c) + spacing
                y_cursor += line_height

        y_cursor = start_y
        for line in lines:
            line_w = sum(font.getlength(c) for c in line) + max(0, len(line)-1) * spacing
            x_cursor = center_x - (line_w / 2)
            for c in line:
                text_draw.text((x_cursor, y_cursor), c, font=font, fill=cfg['color'],
                               stroke_width=cfg['weight'], stroke_fill=cfg['color'])
                x_cursor += font.getlength(c) + spacing
            y_cursor += line_height

    def _create_frame(self, phrase: PhraseContext) -> Path:
        img_path = self.work_dir / f"{phrase.base_name}_frame.png"
        width, height = 1920, 1080
        
        bg_path = Path(self.config['bg_file']) if self.config.get('bg_file') else None
        if bg_path and bg_path.exists():
            base_img = ImageOps.fit(Image.open(bg_path).convert('RGBA'), (width, height), Image.Resampling.LANCZOS)
        else:
            base_img = Image.new('RGBA', (width, height), color=(30, 30, 30, 255))
            
        ru_cfg, de_cfg = self.config['ru'], self.config['de']
        
        try:
            font_de = ImageFont.truetype(de_cfg['font'], de_cfg['size'])
            font_ru = ImageFont.truetype(ru_cfg['font'], ru_cfg['size'])
        except IOError as e:
            raise RuntimeError(f"Ошибка загрузки шрифта: {e}")

        max_w = width - 300
        lines_de = self._wrap_text_spaced(phrase.de_text, font_de, max_w, de_cfg['spacing'])
        lines_ru = self._wrap_text_spaced(phrase.ru_text, font_ru, max_w, ru_cfg['spacing'])
        
        def get_total_height(lines, font):
            if not lines: return 0
            bbox = font.getbbox("Ay")
            return len(lines) * (bbox[3] - bbox[1] + 15)

        h_de = get_total_height(lines_de, font_de)
        h_ru = get_total_height(lines_ru, font_ru)
        
        margin = 40
        half_h = height / 2
        center_x = width / 2

        # Вычисление Y для немецкого языка (0 - 540)
        align_de = de_cfg['align']
        if align_de == 0:   # Сверху
            y_de = margin
        elif align_de == 1: # По центру
            y_de = (half_h - h_de) / 2
        else:               # Снизу
            y_de = half_h - h_de - margin

        # Вычисление Y для русского языка (540 - 1080)
        align_ru = ru_cfg['align']
        if align_ru == 0:   # Сверху
            y_ru = half_h + margin
        elif align_ru == 1: # По центру
            y_ru = half_h + (half_h - h_ru) / 2
        else:               # Снизу
            y_ru = height - h_ru - margin

        shadow_img = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
        s_draw = ImageDraw.Draw(shadow_img)
        self._draw_shadow_layer(s_draw, center_x, y_de, lines_de, font_de, de_cfg['shadow'], de_cfg['spacing'])
        self._draw_shadow_layer(s_draw, center_x, y_ru, lines_ru, font_ru, ru_cfg['shadow'], ru_cfg['spacing'])
        
        if de_cfg['shadow']['blur'] > 0 or ru_cfg['shadow']['blur'] > 0:
            blur_radius = max(de_cfg['shadow']['blur'], ru_cfg['shadow']['blur'])
            shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(blur_radius))
        base_img.alpha_composite(shadow_img)

        text_img = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
        t_draw = ImageDraw.Draw(text_img)
        self._draw_text_two_pass(t_draw, center_x, y_de, lines_de, font_de, de_cfg, de_cfg['spacing'])
        self._draw_text_two_pass(t_draw, center_x, y_ru, lines_ru, font_ru, ru_cfg, ru_cfg['spacing'])
        base_img.alpha_composite(text_img)
        
        base_img.convert('RGB').save(img_path)
        return img_path

    def _build_segment(self, phrase: PhraseContext, img_path: Path, ru_audio: Path, de_audio: Path) -> Path:
        out_video = self.work_dir / f"{phrase.base_name}_segment.mp4"
        cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", "1", "-i", str(img_path)]
        
        filters, concat_nodes = [], []
        input_index = 1
        
        def add_sequence(lang_cfg, audio_path, prefix):
            nonlocal input_index
            repeats = lang_cfg['repeats']
            inner_pause = lang_cfg['inner_pause']
            
            for i in range(repeats):
                cmd.extend(["-i", str(audio_path)])
                node = f"{prefix}_{i}"
                filters.append(f"[{input_index}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[{node}]")
                concat_nodes.append(f"[{node}]")
                input_index += 1
                
                if i < repeats - 1 and inner_pause > 0:
                    p_node = f"p_{prefix}_{i}"
                    filters.append(f"anullsrc=r=44100:channel_layout=stereo:d={inner_pause}[{p_node}]")
                    concat_nodes.append(f"[{p_node}]")

        if self.config['order_ru_first']:
            add_sequence(self.config['ru'], ru_audio, 'ru')
            if self.config['ru']['repeats'] > 0 and self.config['de']['repeats'] > 0 and self.config['main_pause'] > 0:
                filters.append(f"anullsrc=r=44100:channel_layout=stereo:d={self.config['main_pause']}[main_pause]")
                concat_nodes.append("[main_pause]")
            add_sequence(self.config['de'], de_audio, 'de')
        else:
            add_sequence(self.config['de'], de_audio, 'de')
            if self.config['de']['repeats'] > 0 and self.config['ru']['repeats'] > 0 and self.config['main_pause'] > 0:
                filters.append(f"anullsrc=r=44100:channel_layout=stereo:d={self.config['main_pause']}[main_pause]")
                concat_nodes.append("[main_pause]")
            add_sequence(self.config['ru'], ru_audio, 'ru')

        nodes_count = len(concat_nodes)
        if nodes_count > 1:
            filters.append("".join(concat_nodes) + f"concat=n={nodes_count}:v=0:a=1[out_a]")
        elif nodes_count == 1:
            filters.append(f"{concat_nodes[0]}anull[out_a]")
        else:
            filters.append("anullsrc=r=44100:channel_layout=stereo:d=3[out_a]")

        cmd.extend([
            "-filter_complex", ";".join(filters),
            "-map", "0:v", "-map", "[out_a]",
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-shortest", str(out_video)
        ])
        
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"FFmpeg error:\n{e.stderr}")
        return out_video

    def _concatenate_segments(self, segments: list[Path], output_file: Path):
        concat_list = self.work_dir / "concat_list.txt"
        with concat_list.open("w", encoding="utf-8") as f:
            for seg in segments: f.write(f"file '{seg.name}'\n")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output_file)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ColorPickerButton(QPushButton):
    def __init__(self, color=(255, 255, 255, 255)):
        super().__init__()
        self.color = QColor(*color)
        self.update_style()
        self.clicked.connect(self.choose_color)

    def update_style(self):
        css_color = f"rgba({self.color.red()}, {self.color.green()}, {self.color.blue()}, {self.color.alpha() / 255.0})"
        self.setStyleSheet(f"background-color: {css_color}; border: 1px solid #888; border-radius: 4px;")
        self.setText(f"{self.color.red()},{self.color.green()},{self.color.blue()}|{self.color.alpha()}")

    def choose_color(self):
        color = QColorDialog.getColor(self.color, self, "Выберите цвет", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.color = color
            self.update_style()
            
    def get_rgba(self):
        return (self.color.red(), self.color.green(), self.color.blue(), self.color.alpha())

    def set_rgba(self, rgba):
        self.color = QColor(*rgba)
        self.update_style()


class LanguageConfigGroup(QGroupBox):
    def __init__(self, title, def_size, def_color, def_repeats, voices_list, def_align):
        super().__init__(title)
        main_layout = QVBoxLayout()
        
        # --- Подгруппа Текст ---
        text_group = QGroupBox("Текст")
        text_layout = QGridLayout()
        
        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.setToolTip("Для настоящего жирного шрифта выберите файл с суффиксом 'bd' или 'b' (например, arialbd.ttf)")
        self.load_system_fonts()
        self.btn_browse_font = QPushButton("📂"); self.btn_browse_font.setFixedWidth(30)
        self.btn_browse_font.clicked.connect(self.browse_font)

        self.spin_size = QSpinBox(); self.spin_size.setRange(20, 200); self.spin_size.setValue(def_size)
        self.spin_spacing = QSpinBox(); self.spin_spacing.setRange(-50, 100); self.spin_spacing.setValue(0)
        
        self.spin_weight = QSpinBox(); self.spin_weight.setRange(0, 30); self.spin_weight.setValue(0)
        self.spin_outline = QSpinBox(); self.spin_outline.setRange(0, 30); self.spin_outline.setValue(2)
        
        self.btn_color = ColorPickerButton(def_color)
        self.btn_outline_color = ColorPickerButton((0, 0, 0, 255))
        
        self.combo_align = QComboBox()
        self.combo_align.addItems(["Сверху", "По центру", "Снизу"])
        self.combo_align.setCurrentIndex(def_align)
        
        box_size = QHBoxLayout(); box_size.setContentsMargins(0,0,0,0)
        box_size.addWidget(self.spin_size); box_size.addWidget(QLabel("Интервал:")); box_size.addWidget(self.spin_spacing)
        
        box_stroke = QHBoxLayout(); box_stroke.setContentsMargins(0,0,0,0)
        box_stroke.addWidget(self.spin_outline); box_stroke.addWidget(self.btn_outline_color)

        text_layout.addWidget(QLabel("Файл шрифта:"), 0, 0)
        text_layout.addWidget(self.font_combo, 0, 1)
        text_layout.addWidget(self.btn_browse_font, 0, 2)
        
        text_layout.addWidget(QLabel("Размер (px):"), 1, 0)
        text_layout.addLayout(box_size, 1, 1, 1, 2)
        
        text_layout.addWidget(QLabel("Синт. жирность:"), 2, 0)
        text_layout.addWidget(self.spin_weight, 2, 1, 1, 2)
        
        text_layout.addWidget(QLabel("Обводка (px):"), 3, 0)
        text_layout.addLayout(box_stroke, 3, 1, 1, 2)
        
        text_layout.addWidget(QLabel("Цвет текста:"), 4, 0)
        text_layout.addWidget(self.btn_color, 4, 1, 1, 2)
        
        text_layout.addWidget(QLabel("Позиционирование:"), 5, 0)
        text_layout.addWidget(self.combo_align, 5, 1, 1, 2)
        text_group.setLayout(text_layout)
        
        # --- Подгруппа Тень ---
        shadow_group = QGroupBox("Тень / Ореол")
        shadow_layout = QGridLayout()
        
        self.btn_shadow_color = ColorPickerButton((0, 0, 0, 180))
        self.spin_sh_x = QSpinBox(); self.spin_sh_x.setRange(-200, 200); self.spin_sh_x.setValue(5)
        self.spin_sh_y = QSpinBox(); self.spin_sh_y.setRange(-200, 200); self.spin_sh_y.setValue(5)
        self.spin_sh_size = QSpinBox(); self.spin_sh_size.setRange(0, 100); self.spin_sh_size.setValue(0)
        self.spin_sh_blur = QDoubleSpinBox(); self.spin_sh_blur.setRange(0.0, 50.0); self.spin_sh_blur.setValue(2.0)
        
        box_sh_pos = QHBoxLayout(); box_sh_pos.setContentsMargins(0,0,0,0)
        box_sh_pos.addWidget(self.spin_sh_x); box_sh_pos.addWidget(QLabel("Y:")); box_sh_pos.addWidget(self.spin_sh_y)
        
        box_sh_fx = QHBoxLayout(); box_sh_fx.setContentsMargins(0,0,0,0)
        box_sh_fx.addWidget(self.spin_sh_blur); box_sh_fx.addWidget(QLabel("Ореол:")); box_sh_fx.addWidget(self.spin_sh_size)

        shadow_layout.addWidget(QLabel("Цвет (с альфа):"), 0, 0)
        shadow_layout.addWidget(self.btn_shadow_color, 0, 1)
        shadow_layout.addWidget(QLabel("Смещение X:"), 1, 0)
        shadow_layout.addLayout(box_sh_pos, 1, 1)
        shadow_layout.addWidget(QLabel("Размытие:"), 2, 0)
        shadow_layout.addLayout(box_sh_fx, 2, 1)
        shadow_group.setLayout(shadow_layout)

        # --- Подгруппа Аудио ---
        audio_group = QGroupBox("Аудио")
        audio_layout = QFormLayout()
        
        self.combo_voice = QComboBox()
        self.combo_voice.addItems(voices_list)
        
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.5, 2.0)
        self.spin_speed.setSingleStep(0.1)
        self.spin_speed.setValue(1.0)
        self.spin_speed.setSuffix("x")
        
        self.spin_repeats = QSpinBox(); self.spin_repeats.setRange(0, 50); self.spin_repeats.setValue(def_repeats)
        self.spin_inner_pause = QDoubleSpinBox(); self.spin_inner_pause.setRange(0.0, 20.0); self.spin_inner_pause.setValue(1.0)
        
        audio_layout.addRow("Голос:", self.combo_voice)
        audio_layout.addRow("Скорость речи:", self.spin_speed)
        audio_layout.addRow("Повторы:", self.spin_repeats)
        audio_layout.addRow("Пауза (внутр.):", self.spin_inner_pause)
        audio_group.setLayout(audio_layout)

        main_layout.addWidget(text_group)
        main_layout.addWidget(shadow_group)
        main_layout.addWidget(audio_group)
        self.setLayout(main_layout)

    def load_system_fonts(self):
        sys_fonts = Path("C:/Windows/Fonts")
        if sys_fonts.exists():
            fonts = [f.name for f in sys_fonts.glob("*.ttf")]
            self.font_combo.addItems(sorted(fonts))
            if "arial.ttf" in fonts: self.font_combo.setCurrentText("arial.ttf")

    def browse_font(self):
        f, _ = QFileDialog.getOpenFileName(self, "Выбрать шрифт", "C:/Windows/Fonts", "TrueType Шрифты (*.ttf *.otf)")
        if f: self.font_combo.setCurrentText(f)

    def get_state(self):
        return {
            'font': self.font_combo.currentText(),
            'size': self.spin_size.value(),
            'spacing': self.spin_spacing.value(),
            'weight': self.spin_weight.value(),
            'outline_w': self.spin_outline.value(),
            'align': self.combo_align.currentIndex(),
            'color': self.btn_color.get_rgba(),
            'outline_color': self.btn_outline_color.get_rgba(),
            'shadow': {
                'color': self.btn_shadow_color.get_rgba(),
                'x': self.spin_sh_x.value(),
                'y': self.spin_sh_y.value(),
                'size': self.spin_sh_size.value(),
                'blur': self.spin_sh_blur.value()
            },
            'voice': self.combo_voice.currentText(),
            'speed': self.spin_speed.value(),
            'repeats': self.spin_repeats.value(),
            'inner_pause': self.spin_inner_pause.value()
        }

    def set_state(self, state):
        if not state: return
        if 'font' in state: self.font_combo.setCurrentText(state['font'])
        if 'size' in state: self.spin_size.setValue(state['size'])
        if 'spacing' in state: self.spin_spacing.setValue(state['spacing'])
        if 'weight' in state: self.spin_weight.setValue(state['weight'])
        if 'outline_w' in state: self.spin_outline.setValue(state['outline_w'])
        if 'align' in state: self.combo_align.setCurrentIndex(state['align'])
        if 'color' in state: self.btn_color.set_rgba(state['color'])
        if 'outline_color' in state: self.btn_outline_color.set_rgba(state['outline_color'])
        if 'shadow' in state:
            sh = state['shadow']
            if 'color' in sh: self.btn_shadow_color.set_rgba(sh['color'])
            if 'x' in sh: self.spin_sh_x.setValue(sh['x'])
            if 'y' in sh: self.spin_sh_y.setValue(sh['y'])
            if 'size' in sh: self.spin_sh_size.setValue(sh['size'])
            if 'blur' in sh: self.spin_sh_blur.setValue(sh['blur'])
        if 'voice' in state: self.combo_voice.setCurrentText(state['voice'])
        if 'speed' in state: self.spin_speed.setValue(state['speed'])
        if 'repeats' in state: self.spin_repeats.setValue(state['repeats'])
        if 'inner_pause' in state: self.spin_inner_pause.setValue(state['inner_pause'])
        
    def get_config(self):
        state = self.get_state()
        f_val = state['font']
        font_path = Path("C:/Windows/Fonts") / f_val if not Path(f_val).is_absolute() else Path(f_val)
        state['font'] = str(font_path)
        return state


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Генератор Видео-словаря (FFmpeg & Edge-TTS)")
        self.setMinimumWidth(850)
        self.settings_file = Path("settings.json")
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        v_layout = QVBoxLayout(main_widget)

        # 1. IO Блок
        io_group = QGroupBox("Файлы")
        io_layout = QGridLayout()
        
        self.edit_input = QLineEdit()
        btn_input = QPushButton("Выбрать TXT")
        btn_input.clicked.connect(lambda: self._browse_file(self.edit_input, "Текст (*.txt)"))
        
        self.edit_bg = QLineEdit()
        btn_bg = QPushButton("Выбрать Фон")
        btn_bg.clicked.connect(lambda: self._browse_file(self.edit_bg, "Изображения (*.jpg *.png)"))
        
        self.edit_output = QLineEdit("final_output.mp4")
        btn_output = QPushButton("Выбрать сохранение")
        btn_output.clicked.connect(lambda: self._browse_save_file(self.edit_output, "Видео (*.mp4)"))
        
        io_layout.addWidget(QLabel("Исходный файл:"), 0, 0)
        io_layout.addWidget(self.edit_input, 0, 1)
        io_layout.addWidget(btn_input, 0, 2)
        
        io_layout.addWidget(QLabel("Фоновая картинка:"), 1, 0)
        io_layout.addWidget(self.edit_bg, 1, 1)
        io_layout.addWidget(btn_bg, 1, 2)
        
        io_layout.addWidget(QLabel("Выходной файл:"), 2, 0)
        io_layout.addWidget(self.edit_output, 2, 1)
        io_layout.addWidget(btn_output, 2, 2)
        
        io_group.setLayout(io_layout)
        v_layout.addWidget(io_group)

        # 2. Настройки языков
        lang_layout = QHBoxLayout()
        
        # Немецкий по умолчанию снизу своей половины (индекс 2)
        voices_de = ["de-DE-KatjaNeural", "de-DE-KillianNeural", "de-DE-AmalaNeural", "de-DE-ConradNeural"]
        self.cfg_de = LanguageConfigGroup("Немецкий язык [DE]", 64, (255, 255, 255, 255), 2, voices_de, def_align=2)
        
        # Русский по умолчанию сверху своей половины (индекс 0)
        voices_ru = ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"]
        self.cfg_ru = LanguageConfigGroup("Русский язык [RU]", 48, (210, 210, 210, 255), 1, voices_ru, def_align=0)
        
        lang_layout.addWidget(self.cfg_de)
        lang_layout.addWidget(self.cfg_ru)
        v_layout.addLayout(lang_layout)

        # 3. Сценарий и Очистка
        scen_group = QGroupBox("Сценарий и Система")
        scen_layout = QHBoxLayout()
        
        self.combo_order = QComboBox()
        self.combo_order.addItems(["Сначала Русский [RU -> DE]", "Сначала Немецкий [DE -> RU]"])
        
        self.spin_main_pause = QDoubleSpinBox()
        self.spin_main_pause.setRange(0.0, 30.0)
        self.spin_main_pause.setValue(5.0)
        self.spin_main_pause.setSuffix(" сек")

        self.chk_cleanup = QCheckBox("Очищать папку build_tmp после успешной сборки")
        self.chk_cleanup.setChecked(False)
        
        scen_layout.addWidget(QLabel("Порядок озвучки:"))
        scen_layout.addWidget(self.combo_order)
        scen_layout.addSpacing(20)
        scen_layout.addWidget(QLabel("Главная пауза (между языками):"))
        scen_layout.addWidget(self.spin_main_pause)
        scen_layout.addSpacing(20)
        scen_layout.addWidget(self.chk_cleanup)
        scen_layout.addStretch()
        scen_group.setLayout(scen_layout)
        v_layout.addWidget(scen_group)

        # 4. Управление и Логирование
        btn_layout = QHBoxLayout()
        
        self.btn_reset = QPushButton("↺ Сброс настроек")
        self.btn_reset.setFixedHeight(45)
        self.btn_reset.clicked.connect(self.reset_settings)
        
        self.btn_start = QPushButton("🚀 Начать сборку видео")
        self.btn_start.setFixedHeight(45)
        self.btn_start.setStyleSheet("font-weight: bold; font-size: 14px; background-color: #2b78e4; color: white; border-radius: 4px;")
        self.btn_start.clicked.connect(self.start_processing)
        
        btn_layout.addWidget(self.btn_reset)
        btn_layout.addWidget(self.btn_start, stretch=1)
        v_layout.addLayout(btn_layout)
        
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: Consolas; font-size: 13px;")
        v_layout.addWidget(self.log_console)
        
        self.worker = None
        
        self.default_state = self.get_full_state()
        self.load_settings()

    def _browse_file(self, line_edit, filt):
        f, _ = QFileDialog.getOpenFileName(self, "Выбрать файл", "", filt)
        if f: line_edit.setText(f)
        
    def _browse_save_file(self, line_edit, filt):
        f, _ = QFileDialog.getSaveFileName(self, "Сохранить файл как", line_edit.text(), filt)
        if f: line_edit.setText(f)

    def log(self, msg, color="#00ff00"):
        self.log_console.append(f"<span style='color:{color}'>{msg}</span>")
        self.log_console.verticalScrollBar().setValue(self.log_console.verticalScrollBar().maximum())

    def get_full_state(self):
        return {
            'io': {
                'input': self.edit_input.text(),
                'bg': self.edit_bg.text(),
                'output': self.edit_output.text()
            },
            'scen': {
                'order': self.combo_order.currentIndex(),
                'main_pause': self.spin_main_pause.value(),
                'cleanup': self.chk_cleanup.isChecked()
            },
            'de': self.cfg_de.get_state(),
            'ru': self.cfg_ru.get_state()
        }

    def set_full_state(self, state):
        if not state: return
        if 'io' in state:
            self.edit_input.setText(state['io'].get('input', ''))
            self.edit_bg.setText(state['io'].get('bg', ''))
            self.edit_output.setText(state['io'].get('output', 'final_output.mp4'))
        if 'scen' in state:
            self.combo_order.setCurrentIndex(state['scen'].get('order', 0))
            self.spin_main_pause.setValue(state['scen'].get('main_pause', 5.0))
            self.chk_cleanup.setChecked(state['scen'].get('cleanup', False))
        if 'de' in state:
            self.cfg_de.set_state(state['de'])
        if 'ru' in state:
            self.cfg_ru.set_state(state['ru'])

    def load_settings(self):
        if self.settings_file.exists():
            try:
                state = json.loads(self.settings_file.read_text(encoding='utf-8'))
                self.set_full_state(state)
                self.log("Настройки загружены.", "#aaaaaa")
            except Exception as e:
                self.log(f"Ошибка чтения settings.json. Применены значения по умолчанию. ({e})", "#ff4444")

    def save_settings(self):
        try:
            state = self.get_full_state()
            self.settings_file.write_text(json.dumps(state, indent=4, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            self.log(f"Не удалось сохранить настройки: {e}", "#ff4444")

    def reset_settings(self):
        self.set_full_state(self.default_state)
        self.log("Настройки сброшены к значениям по умолчанию.", "#aaaaaa")
        
    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    def start_processing(self):
        if not self.edit_input.text():
            self.log("ОШИБКА: Не выбран исходный TXT файл!", "#ff4444")
            return
            
        self.save_settings()
            
        config = {
            'input_file': self.edit_input.text(),
            'bg_file': self.edit_bg.text(),
            'output_file': self.edit_output.text() or 'final_output.mp4',
            'main_pause': self.spin_main_pause.value(),
            'order_ru_first': self.combo_order.currentIndex() == 0,
            'cleanup_tmp': self.chk_cleanup.isChecked(),
            'de': self.cfg_de.get_config(),
            'ru': self.cfg_ru.get_config()
        }

        self.btn_start.setEnabled(False)
        self.btn_reset.setEnabled(False)
        self.log("Инициализация сборки...", "#ffffff")
        
        self.worker = BuilderThread(config)
        self.worker.log_signal.connect(self.log)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.error_signal.connect(self.on_error)
        self.worker.start()

    def on_finished(self, msg):
        self.log(msg, "#00aaff")
        self.btn_start.setEnabled(True)
        self.btn_reset.setEnabled(True)

    def on_error(self, err_msg):
        self.log(f"КРИТИЧЕСКАЯ ОШИБКА: {err_msg}", "#ff4444")
        self.btn_start.setEnabled(True)
        self.btn_reset.setEnabled(True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())