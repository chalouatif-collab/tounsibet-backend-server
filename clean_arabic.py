import re
import os

def clean_arabic_comments(filepath):
    if not os.path.exists(filepath):
        print(f"Le fichier {filepath} n'existe pas.")
        return

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. مسح تعليقات HTML التي تحتوي على عربي
    content = re.sub(r'<!--[\s\S]*?[\u0600-\u06FF]+[\s\S]*?-->\n?', '', content)
    
    # 2. مسح تعليقات الجافاسكريبت (السطر الواحد //) التي تحتوي على عربي
    content = re.sub(r'^[ \t]*//.*[\u0600-\u06FF]+.*\n?', '', content, flags=re.MULTILINE)
    content = re.sub(r'//.*[\u0600-\u06FF]+.*', '', content)
    
    # 3. مسح تعليقات الجافاسكريبت (المتعددة الأسطر /* */) التي تحتوي على عربي
    content = re.sub(r'/\*[\s\S]*?[\u0600-\u06FF]+[\s\S]*?\*/\n?', '', content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
        
    print(f"✅ Nettoyage terminé avec succès pour : {filepath}")

# ضع اسم الملف الذي تريد تنظيفه هنا (سواء index_2.html أو index.html)
clean_arabic_comments('index.html')