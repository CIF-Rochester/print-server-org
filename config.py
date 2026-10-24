
import os
import sys
import configparser
from dataclasses import dataclass

@dataclass
class Logging:
    log_path: os.PathLike
    file_storage_path: os.PathLike
    file_storage_time: int

@dataclass
class Printer:
    color_printer_name: str
    bw_printer_name: str

@dataclass
class PrintLimitations:
    daily_regen: int
    max_points: int
    default_given: int
    color_multiplier: float
    double_sided_multiplier: float

@dataclass
class Database:
    username: str
    password: str
    database: str
    table: str

@dataclass
class IPA:
    username: str
    password: str
    ip: str

@dataclass
class Config:
    logging: Logging
    printer: Printer
    print_limitations: PrintLimitations
    database: Database
    ipa: IPA

def load_config(config_path: os.PathLike):
    try:
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
    except Exception as e:
        print(f"Failed to load config file from {config_path}: {e}", file=sys.stderr)
        exit(1)
    try:
        logging = Logging(log_path=cfg.get("logging","log_path"), file_storage_path=cfg.get("logging","file_storage_path"), file_storage_time=cfg.getint("logging","file_storage_time"))
        printer = Printer(color_printer_name=cfg.get("printer","color_printer_name"), bw_printer_name=cfg.get("printer","bw_printer_name"))
        print_limitations = PrintLimitations(daily_regen=cfg.getint("print_limitations", "daily_regen"), max_points=cfg.getint("print_limitations", "max_points"), default_given=cfg.getint("print_limitations", "default_given"), color_multiplier=cfg.getfloat("print_limitations", "color_multiplier"), double_sided_multiplier=cfg.getfloat("print_limitations", "double_sided_multiplier"))
        database = Database(username=cfg.get("database", "username"), password=cfg.get("database", "password"), database=cfg.get("database", "database"), table=cfg.get("database", "table"))
        ipa = IPA(username=cfg.get("ipa","username"), password=cfg.get("ipa","password"),ip=cfg.get("ipa","ip"))
        config = Config(logging=logging, printer=printer, print_limitations=print_limitations, database=database, ipa=ipa)
    except Exception as e:
        print(f"Error in config file {config_path}: {e}", file=sys.stderr)
        exit(1)
    return config
