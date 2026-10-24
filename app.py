import argparse
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, jsonify
from config import load_config, Config
from python_freeipa import ClientMeta
from python_freeipa.exceptions import Unauthorized
from pyeasyencrypt.pyeasyencrypt import encrypt_string, decrypt_string
from werkzeug.utils import secure_filename
from pypdf import PdfReader
import math
import subprocess
import mariadb
import schedule

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_CFG_PATH = os.path.join(BASE_DIR, "config.cfg")
with open(f'{BASE_DIR}/libreoffice_extensions.txt') as f:
    LIBREOFFICE_EXTENSIONS = [l for l in f.read().split('\n') if l]
with open(f'{BASE_DIR}/imagemagick_extensions.txt') as f:
    IMAGEMAGICK_EXTENSIONS = [l for l in f.read().split('\n') if l]
SUPPORTED_EXTENSIONS = {"pdf"}
SUPPORTED_EXTENSIONS.update(LIBREOFFICE_EXTENSIONS)
SUPPORTED_EXTENSIONS.update(IMAGEMAGICK_EXTENSIONS)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024
logger = None
ipa_server = None


def makeLogger(logFile):
    formatter = logging.Formatter(fmt='[%(asctime)s] %(levelname)-8s %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(logFile)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger

def get_mac_address():
    # Fetch the 48-bit integer representing the MAC address
    mac_num = uuid.getnode()


    # Format the integer into a standard hex string (XX:XX:XX:XX:XX:XX)
    mac_str = ':'.join(['{:02x}'.format((mac_num >> ele) & 0xff) for ele in range(0, 8 * 6, 8)][::-1])
    return mac_str

def authenticate(username, password):
    try:
        ipa_server.login(username, password)
        return True
    except Unauthorized:
        return False

def encrypt_password(password):
    return encrypt_string(password, get_mac_address())

def decrypt_password(e_password):
    return decrypt_string(e_password, get_mac_address())

def print_file(username, file, copies, pages, per_page, color, ornt, sided):
    if color == 'Black and White':
        command = f"lpr -o print-scaling=none -#{copies} -o media=letter -o fit-to-page -o number-up={per_page} -P {config.printer.bw_printer_name} -o Ink=MONO"
    else:
        command = f"lpr -o print-scaling=none -#{copies} -o media=letter -o fit-to-page -o number-up={per_page} -P {config.printer.color_printer_name}"
    if pages:
        command += f" -o pages-ranges={pages}"
    if ornt == 'Landscape':
        command += f" -o orientation-requested=4"
    if sided == 'Double Sided':
        if ornt == 'Landscape':
            command += f" -o sides=two-sided-short-edge"
        else:
            command += f" -o sides=two-sided-long-edge"
    command += f" {file}"
    logger.info(f"Running command \"{command}\"")
    err = subprocess.run(command, shell=True, stderr=subprocess.PIPE).stderr.decode('utf-8')
    if not pages:
        pages = 'ALL'
    log_output = f"{username} - File: {file.split('/')[-1]}  Copies:{copies} Pages:{pages} Pages/sheet:{per_page} Color:{color} Orientation:{ornt} Sided:{sided}"
    if err:
        log_output += " FAILED"
    logger.info(log_output)
    if err:
        #logger.error(err)
        return err
    return ""

def subtract_user_points(username, num):
    database = mariadb.connect(host='localhost', user=config.database.username, password=config.database.password,
                               database=config.database.database)
    cursor = database.cursor()
    points = get_user_points(username, database, cursor)
    cursor.execute(f"UPDATE print_points SET points = {points - num} WHERE netid = '{username}'")
    database.commit()
    database.close()

def get_user_points(username, database=None, cursor=None):
    new_conn = database is None
    if new_conn:
        database = mariadb.connect(host='localhost', user=config.database.username, password=config.database.password, database=config.database.database)
        cursor = database.cursor()
    cursor.execute(f"SELECT * FROM {config.database.table} WHERE netid = '{username}'")
    output = cursor.fetchone()
    if output is None:
        cursor.execute(f"INSERT INTO {config.database.table} VALUE ('{username}', {config.print_limitations.default_given})")
        database.commit()
        if new_conn:
            database.close()
        return config.print_limitations.default_given
    if new_conn:
        database.close()
    return int(output[1])

def update_points():
    database = mariadb.connect(host='localhost', user=config.database.username, password=config.database.password, database=config.database.database)
    cursor = database.cursor()
    cursor.execute(f"SELECT * FROM {config.database.table}")
    output = cursor.fetchall()
    for line in output:
        points = int(line[1])
        cursor.execute(f"UPDATE print_points SET points = {min(points + 16, config.print_limitations.max_points)} WHERE netid = '{line[0]}'")
    database.commit()
    database.close()

def scheduler():
    schedule.every().day.at("00:00").do(update_points)
    while True:
        schedule.run_pending()
        time.sleep(60)

def convert_files(username, files):
    new_files = []
    os.makedirs(f"{BASE_DIR}/tmp/{username}", exist_ok=True)
    for file in os.listdir(f"{BASE_DIR}/tmp/{username}"):
        os.remove(f"{BASE_DIR}/tmp/{username}/{file}")
    for file in files:
        current_time = datetime.now()
        time_string_file = current_time.strftime('%H.%M.%S_%m-%d-%Y')
        extension = file.filename.split('.')[-1]
        filename = f"{BASE_DIR}/tmp/{username}/{time_string_file}---{secure_filename(file.filename)}"
        file.save(filename)
        err = ""
        if extension in LIBREOFFICE_EXTENSIONS:
            err = subprocess.run(f"libreoffice --headless --convert-to pdf {filename} --outdir {BASE_DIR}/tmp/{username}", shell=True, stderr=subprocess.PIPE).stderr.decode('utf-8')
            os.remove(filename)
            new_files.append(f"{'.'.join(filename.split('.')[:-1])}.pdf")
        elif extension in IMAGEMAGICK_EXTENSIONS:
            pdf_name = f"{'.'.join(filename.split('.')[:-1])}.pdf"
            err = subprocess.run(f"convert {filename} {pdf_name}", shell=True, stderr=subprocess.PIPE).stderr.decode('utf-8')
            os.remove(filename)
            new_files.append(pdf_name)
        elif extension == "pdf":
            new_files.append(filename)
        else:
            output = subprocess.run(f"file {filename}", shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
            if not "ASCII text" in output:
                return f"Unsupported file type {filename.split('.')[-1]}"
            err = subprocess.run(f"libreoffice --headless --convert-to pdf {filename} --outdir {BASE_DIR}/tmp/{username}", shell=True, stderr=subprocess.PIPE).stderr.decode('utf-8')
            os.remove(filename)
            new_files.append(f"{'.'.join(filename.split('.')[:-1])}.pdf")
        if err:
            for file in os.listdir(f"{BASE_DIR}/tmp/{username}"):
                os.remove(f"{BASE_DIR}/tmp/{username}/{file}")
            os.rmdir(f"{BASE_DIR}/tmp/{username}")
            return err
    return new_files

def update_storage(username):
    new_files = []
    for file in os.listdir(f"{BASE_DIR}/tmp/{username}"):
        if file == 'print-job':
            continue
        shutil.move(f"{BASE_DIR}/tmp/{username}/{file}", f"{config.logging.file_storage_path}/{file}")
        new_files.append(f"{config.logging.file_storage_path}/{file}")
    current_time = datetime.now()
    files = os.listdir(config.logging.file_storage_path)
    for file in files:
        if (current_time - datetime.strptime(file[:file.index("---")],
                                             '%H.%M.%S_%m-%d-%Y')).total_seconds() > config.logging.file_storage_time:
            os.remove(f"{config.logging.file_storage_path}/{file}")
    return new_files

def parse_pages(pages):
    page_arr = []
    for page_spl in pages.split(','):
        if page_spl.isnumeric():
            if int(page_spl) <= 0:
                return None
            page_arr.append(int(page_spl))
        else:
            page_spl_range = page_spl.split('-')
            if not len(page_spl_range) == 2 or not page_spl_range[0].isnumeric() or int(page_spl_range[0]) <= 0 or not page_spl_range[1].isnumeric() or int(page_spl_range[1]) <= 0:
                return None
            for i in range(int(page_spl_range[0]), int(page_spl_range[1]) + 1):
                page_arr.append(i)
    return page_arr

def get_num_pages_printed(file, copies, pages, per_page, sided):
    reader = PdfReader(file)
    pdf_num_pages = len(reader.pages)
    if not pages or pages == 'All pages':
        page_arr = list(range(1, pdf_num_pages + 1))
    else:
        page_arr = parse_pages(pages)
        page_arr = [p for p in page_arr if p <= pdf_num_pages]
    num_pages = len(page_arr) * copies / per_page
    if sided == 'Double Sided':
        num_pages /= 2
    return math.ceil(num_pages)

def get_points_spent(file, color, pages, per_page, copies, sided):
    points = get_num_pages_printed(file, copies, pages, per_page, sided)
    if color == 'Color':
        points *= config.print_limitations.color_multiplier
    if sided == 'Double Sided':
        points *= config.print_limitations.double_sided_multiplier
    return points

@app.route('/check-print', methods=['POST'])
def check_params():
    username = request.args.get('username')
    pages = request.form.get('pages').replace(" ", "")
    if pages and not pages == 'All Pages' and parse_pages(pages) is None:
        return jsonify({"error": "Unable to parse field \"Pages\""}), 400
    color = request.form.get('color')
    ornt = request.form.get('orientation')
    per_page = int(request.form.get('perpage'))
    if per_page <= 0:
        return jsonify({"error": "Invalid value for field \"Pages/sheet\""}), 400
    copies = int(request.form.get('copies'))
    if copies <= 0:
        return jsonify({"error": "Invalid value for field \"Copies\""}), 400
    files = request.files.getlist('file')
    for file in files:
        if not file or file is None:
            return jsonify({"error": "No file selected"}), 400
        if file.filename == 'print-job':
            return jsonify({"error": "Invalid file name", "error2": "\"print-job\" is a reserved file name"}), 400
    sided = request.form.get('sided')
    files = convert_files(username, files)
    if isinstance(files, str):
        return jsonify({"error": f"Unable to convert file type to pdf: {files}"}), 400
    points = 0
    for file in files:
        points += get_points_spent(file, color, pages, per_page, copies, sided)
    if points <= 0:
        return jsonify({"error": f"Cannot submit empty print job"}), 400
    points = int(points)
    total_points = get_user_points(username)
    if points > total_points:
        return jsonify({"error": f"Insufficient print points", "error2": f"Total points:{total_points}  Required points:{points}"}), 400
    with open(f"{BASE_DIR}/tmp/{username}/print-job", 'w') as f:
        f.write(f"{','.join(['.'.join(file.split('/')[-1].split('.')[:-1]) for file in files])}\n{pages}\n{color}\n{ornt}\n{per_page}\n{copies}\n{sided}")
    return jsonify({"total": total_points, "points": points}), 200

@app.route('/print', methods=['GET', 'POST'])
def get_main_page():
    if not "username" in request.args or not "d" in request.args:
        return redirect('/')
    username = request.args.get('username')
    password = decrypt_password(request.args.get('d'))
    if not authenticate(username, password):
        return redirect('/login')
    if request.method == 'GET':
        return render_template('index.html', username=username)
    else:
        pages = request.form.get('pages').replace(" ", "")
        color = request.form.get('color')
        ornt = request.form.get('orientation')
        per_page = int(request.form.get('perpage'))
        copies = int(request.form.get('copies'))
        files = request.files.getlist('file')
        sided = request.form.get('sided')
        files = update_storage(username)
        if not os.path.exists(f"{BASE_DIR}/tmp/{username}/print-job"):
            return render_template('error.html', error="Print job does not match confirmed print settings", home_url=request.full_path)
        with open(f"{BASE_DIR}/tmp/{username}/print-job", 'r') as f:
            job_check = f.read().split('\n')
        if len(job_check) < 1:
            return render_template('error.html', error="Print job does not match verified print settings",home_url=request.full_path)
        line1 = job_check[0].split(',')
        print(job_check)
        for file in files:
            if not '.'.join(file.split('/')[-1].split('.')[:-1]) in line1:
                return render_template('error.html', error="Print job does not match verified print settings",home_url=request.full_path)
        if len(job_check) < 7 or job_check[1] != str(pages) or job_check[2] != str(color) or job_check[3] != str(ornt) or job_check[4] != str(per_page) or job_check[5] != str(copies) or job_check[6] != str(sided):
            return render_template('error.html', error="Print job does not match verified print settings", home_url=request.full_path)
        errs = ""
        for file in files:
            return_err = print_file(username, file, copies, pages, per_page, color, ornt, sided)
            if return_err:
                errs += file.split('/')[-1]
                errs += "\n"
            else:
                subtract_user_points(username, get_points_spent(file, color, pages, per_page, copies, sided))
        if len(errs) > 0:
            error_output = f"The following files failed to print.  Points have not been deducted.\n{errs}"
            return render_template('error.html', home_url=request.full_path, error=error_output)
        return render_template('success.html', home_url=request.full_path)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html', error='')
    else:
        username = request.form.get('username')
        password = encrypt_password(request.form.get('password'))
        return redirect(f'/print?username={username}&d={password}')

@app.route('/points', methods=['GET'])
def get_points():
    return render_template('points.html')

@app.route('/', methods=['GET', 'POST'])
def get_page():
    return redirect('/login')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Print server for remotely printing to CIF lab printer")
    parser.add_argument('--config', '-c', help='Path to print server config file.', default=DEFAULT_CFG_PATH)
    args = parser.parse_args()
    path_to_cfg = args.config
    config: Config = load_config(path_to_cfg)
    logger = makeLogger(config.logging.log_path)
    ipa_server = ClientMeta(config.ipa.ip, verify_ssl=False)

    threading.Thread(daemon=True).start()
    thread = threading.Thread(target=scheduler)
    thread.start()
    app.run(host='0.0.0.0', port=5000,debug=False)
