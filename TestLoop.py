import ftplib
import sys
import json
import logging
import psycopg2
import os
import urllib
import gc
import shutil
import time
import ntpath

from PeakListParser import PeakListParser
from MzIdParser import MzIdParser
from MzIdParser import NumpyEncoder
import PostgreSQL as db


class TestLoop:

    def __init__(self):

        self.exclusion_list = []
        #     '2016/04/PXD003564',
        #     '2016/04/PXD003565',
        #     '2016/04/PXD003566',
        #     '2016/04/PXD003567',
        #     '2016/04/PXD003568',
        #     '2016/05/PXD002905',
        #     '2016/10/PXD003935',
        #     '2016/10/PXD004572',
        #     '2017/05/PXD005403',
        #     '2017/06/PXD001767'  # big zip
        # ]
        # logging
        # try:
        #     dev = False
        #     logFile = dname + "/log/%s_%s.log" % (args[2], int(time()))
        #
        # except IndexError:
        #     dev = True
        #     logFile = "log/parser_%s.log" % int(time())
        #
        # try:
        #     os.remove(logFile)
        # except OSError:
        #     pass
        # os.fdopen(os.open(logFile, os.O_WRONLY | os.O_CREAT, 0o777), 'w').close()

        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s %(levelname)s %(name)s %(message)s')
        self.logger = logging.getLogger(__name__)

        self.ip = "193.62.192.9"
        self.base = "pride/data/archive"
        self.mzId_count = 0
        self.unimod_path = 'obo/unimod.obo'

        self.temp_dir = os.path.expanduser('~') + "/parser_temp/"

        # connect to DB
        try:
            con = db.connect('')
            cur = con.cursor()

        except db.DBException as e:
            self.logger.error(e)
            print(e)
            sys.exit(1)

        # # create Database tables
        # try:
        #     db.create_tables(cur, con)
        # except db.DBException as e:
        #     self.logger.error(e)
        #     print(e)
        #     sys.exit(1)

        con.close

    def all_years(self):
        files = self.get_ftp_file_list(self.base)
        for f in files:
            self.year(f)

    def year(self, y):
        target_dir = self.base + '/' + y
        files = self.get_ftp_file_list(target_dir)
        for f in files:
            self.month(y + '/' + f)

    def month(self, ym):
        target_dir = self.base + '/' + ym
        files = self.get_ftp_file_list(target_dir)
        for f in files:
            ymp = ym + '/' + f
            if ymp not in self.exclusion_list:
                self.project(ymp)
            else:
                print('skipping ' + ymp)

    def project(self, ymp):
        pxd = ymp.split('/')[-1]
        # todo: defend against not getting response from pride api
        pride = TestLoop.get_pride_info(pxd)

        if pride['submissionType'] == 'COMPLETE':
            target_dir = self.base + '/' + ymp
            files = self.get_ftp_file_list(target_dir)
            print ('>> ' + ymp)

            for f in files:
                if f.lower().endswith('mzid') or f.lower().endswith('mzid.gz'):
                    print(f)
                    self.file(ymp, f)
                    break

    def file(self, ymp, file_name):
        #  make temp dir
        try:
            os.mkdir(self.temp_dir)
        except OSError:
            pass

        path = self.temp_dir + file_name
        target_dir = '/' + self.base + '/' + ymp
        ftp = self.get_ftp_login()

        # fetch mzId file from pride
        try:
            ftp.cwd(target_dir)
            ftp.retrbinary("RETR " + file_name, open(path, 'wb').write)
        except ftplib.error_perm as e:
            ftp.quit()
            error_msg = "%s: %s" % (file_name, e.args[0])
            self.logger.error(error_msg)
            raise e
        ftp.quit()

        # init parser
        try:
            mzId_parser = MzIdParser(path, self.temp_dir, 5, db, self.logger, origin=ymp)
        except Exception as mzId_error:
            error = json.dumps(mzId_error.args, cls=NumpyEncoder)

            con = db.connect('')
            cur = con.cursor()
            try:
                cur.execute("""
                        INSERT INTO uploads (
                            user_id,
                            origin,
                            filename,
                            error_type,
                            upload_error)
                        VALUES (%s, %s, %s, %s, %s)""",
                            [5, ymp, file_name, type(mzId_error).__name__, error])
                con.commit()
            except psycopg2.Error as e:
                raise db.DBException(e.message)
            con.close()
            return

        try:
            # write upload info to db
            mzId_parser.upload_info()
        except Exception as mzId_error:
            self.logger.exception(mzId_error)

            error = json.dumps(mzId_error.args, cls=NumpyEncoder)

            con = db.connect('')
            cur = con.cursor()
            try:
                cur.execute("""
                        INSERT INTO uploads (
                            user_id,
                            origin,
                            filename,
                            error_type,
                            upload_error)
                        VALUES (%s, %s, %s, %s, %s)""", [5, ymp, file_name, type(mzId_error).__name__, error])
                con.commit()

            except psycopg2.Error as e:
                raise db.DBException(e.message)
            con.close()
            return

        # fetch peak list files from pride
        peak_files = mzId_parser.get_peak_list_file_names()
        for peak_file in peak_files:
            # peak_file = ntpath.basename(peak_file)

            if peak_file == '':
                ftp.close()
                print('Spectra data missing location att')
                warnings = json.dumps(mzId_parser.warnings, cls=NumpyEncoder)
                con = db.connect('')
                cur = con.cursor()
                try:
                    cur.execute("""
                    UPDATE uploads SET
                        error_type=%s,
                        upload_error=%s,
                        upload_warnings=%s
                    WHERE id = %s""", ['Spectra data missing location att?', '', warnings, mzId_parser.upload_id])
                    con.commit()
                except psycopg2.Error as e:
                    raise db.DBException(e.message)
                con.close()
                return

            ftp = self.get_ftp_login()
            try:
                ftp.cwd(target_dir)
                print('getting ' + peak_file)
                ftp.retrbinary("RETR " + peak_file,
                               open(self.temp_dir + peak_file, 'wb').write)
            except ftplib.error_perm as e:
                print('missing file: ' + peak_file + " (checking for .gz)")
                #  check for gzipped
                try:
                    os.remove(self.temp_dir + peak_file)
                    print('getting ' + peak_file + '.gz')
                    # ftp.cwd(target_dir + '/generated/')
                    ftp.retrbinary("RETR " + peak_file + '.gz',
                                   open(self.temp_dir + '/' + peak_file + '.gz', 'wb').write)
                except ftplib.error_perm as e:
                    ftp.close()
                    print('missing file: ' + peak_file + '.gz')

                    warnings = json.dumps(mzId_parser.warnings, cls=NumpyEncoder)

                    con = db.connect('')
                    cur = con.cursor()
                    try:
                        cur.execute("""
                        UPDATE uploads SET
                            error_type=%s,
                            upload_error=%s,
                            upload_warnings=%s
                        WHERE id = %s""", ["Missing file?", peak_file, warnings, mzId_parser.upload_id])
                        con.commit()
                    except psycopg2.Error as e:
                        raise db.DBException(e.message)
                    con.close()
                    return
                ftp.close()
                # peak_file = ntpath.basename(
                #     PeakListParser.extract_gz(self.temp_dir + '/' + peak_file + '.gz')[0])
            ftp.close()

        # actually parse
        try:
            mzId_parser.parse()
        except Exception as mzId_error:
            self.logger.exception(mzId_error)

            error = json.dumps(mzId_error.args, cls=NumpyEncoder)
            mzId_parser.mzid_reader.reset()
            spectra_formats = json.dumps(mzId_parser.mzid_reader.iterfind('SpectraData').next(), cls=NumpyEncoder)
            mzId_parser.mzid_reader.reset()

            warnings = json.dumps(mzId_parser.warnings, cls=NumpyEncoder)

            con = db.connect('')
            cur = con.cursor()
            try:
                cur.execute("""
            UPDATE uploads SET
                error_type=%s,
                upload_error=%s,
                spectra_formats=%s,
                upload_warnings=%s
            WHERE id = %s""", [type(mzId_error).__name__, error, spectra_formats, warnings, mzId_parser.upload_id])
                con.commit()

            except psycopg2.Error as e:
                raise db.DBException(e.message)
            con.close()

        try:
            shutil.rmtree(self.temp_dir)
        except OSError:
            pass
        self.mzId_count = self.mzId_count + 1
        mzId_parser = None
        gc.collect()

    def get_ftp_login(self):
        try:
            ftp = ftplib.FTP(self.ip)
            ftp.login()  # Uses password: anonymous@
            return ftp
        except:
            print('FTP fail... giving it a few secs...')
            time.sleep(200)
            return self.get_ftp_login()

    def get_ftp_file_list (self, dir):
        ftp = self.get_ftp_login()
        try:
            ftp.cwd(dir)
        except ftplib.error_perm as e:
            error_msg = "%s: %s" % (dir, e.args[0])
            print error_msg
            ftp.quit()
            return []

        files = []

        try:
            files = ftp.nlst()
        except ftplib.error_perm, resp:
            if str(resp) == "550 No files found":
                print "No files in this directory"
            else:
                error_msg = "%s: %s" % (dir, ftplib.error_perm.args[0])
                print error_msg

        ftp.quit()
        files.reverse()
        return files

    @staticmethod
    def get_pride_info (pxd):
        time.sleep(1)
        try:
            prideAPI = urllib.urlopen('https://www.ebi.ac.uk:443/pride/ws/archive/project/' + pxd).read()
            pride = json.loads(prideAPI)
            return pride
        except Exception:
            print ("failed to get " + pxd + "from pride api. Will try again in 5 secs.")
            time.sleep(5)
            return TestLoop.get_pride_info(pxd)


test_loop = TestLoop()


# test_loop.year('2018')
# test_loop.year('2017')
# test_loop.year('2016')
# test_loop.year('2015')

# test_loop.month('2012/12')
# test_loop.year('2013')
# test_loop.year('2014')

# crashed at >> 2014/11/PXD001267
# F100626.mzid.gz
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 165, in file
#     mzId_parser.upload_info()
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/MzIdParser.py", line 737, in upload_info
#     peak_list_file_names = json.dumps(self.get_peak_list_file_names(), cls=NumpyEncoder)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/MzIdParser.py", line 98, in get_peak_list_file_names
#     sp_datum = self.mzid_reader.get_by_id(spectra_data_id, tag_id='SpectraData', detailed=True)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/python_env/local/lib/python2.7/site-packages/pyteomics/xml.py", line 65, in wrapped
#     return func(self, *args, **kwargs)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/python_env/local/lib/python2.7/site-packages/pyteomics/xml.py", line 992, in get_by_id
#     elem = self._find_by_id_reset(elem_id, id_key=id_key)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/python_env/local/lib/python2.7/site-packages/pyteomics/xml.py", line 65, in wrapped
#     return func(self, *args, **kwargs)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/python_env/local/lib/python2.7/site-packages/pyteomics/xml.py", line 957, in _find_by_id_reset
#     return self._find_by_id_no_reset(elem_id, id_key=id_key)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/python_env/local/lib/python2.7/site-packages/pyteomics/xml.py", line 515, in _find_by_id_no_reset
#     self._source, events=('start', 'end'), remove_comments=True):
#   File "src/lxml/iterparse.pxi", line 208, in lxml.etree.iterparse.__next__ (src/lxml/etree.c:155963)
#   File "/home/col/parser_temp/F100626.mzid", line 28551
# lxml.etree.XMLSyntaxError: Input is not proper UTF-8, indicate encoding !
# Bytes: 0xE9 0x65 0x73 0x20, line 28551, column 94

# could be some missing from 2014/11

# test_loop.month('2014/12')

# crashed at >> 2015/02/PXD000164 - I unplugged net cable

# test_loop.year('2015')

# test_loop.month('2015/02') - problems do this month again

# test_loop.month('2015/03') # crash, >> 2015/03/PXD000980

# test_loop.month('2015/04')
# test_loop.month('2015/05')

# crash at 2015/05/PXD001428 (out of memory / thrashing)

# test_loop.month('2015/06')
# test_loop.month('2015/07')
# test_loop.month('2015/08')
# test_loop.month('2015/09')
# test_loop.month('2015/10')
# test_loop.month('2015/11')
# test_loop.month('2015/12')

# crash at >> 2018/04/PXD008493 (out of memory / thrashing)

# test_loop.project("2017/12/PXD006591")




# mzML
# test_loop.project("2017/11/PXD007748")
# test_loop.project("2016/11/PXD004785")
# test_loop.project("2016/05/PXD002967")
# test_loop.project("2016/09/PXD004499")
# test_loop.project("2015/06/PXD002045")
# test_loop.project("2017/08/PXD007149")
# test_loop.project("2015/06/PXD002048")
test_loop.project("2015/06/PXD002047")
# 2015/06/PXD002046
# 2014/09/PXD001006
# 2014/09/PXD001000
# 2016/09/PXD002317
# 2014/09/PXD000966
# 2015/06/PXD002044
# 2015/06/PXD002043
# 2015/06/PXD002042
# 2015/06/PXD002041
# 2016/06/PXD004163
# 2015/05/PXD002161
# 2018/01/PXD007913
# 2017/11/PXD006204
# 2015/07/PXD002089
# 2015/07/PXD002088
# 2015/07/PXD002087
# 2015/07/PXD002086
# 2017/07/PXD002901
# 2015/07/PXD002085
# 2017/11/PXD007689
# 2015/07/PXD002084
# 2015/05/PXD002161
# 2015/05/PXD002161
# 2015/07/PXD002083
# 2015/07/PXD002082
# 2015/07/PXD002081
# 2015/07/PXD002080
# 2015/06/PXD002050
# 2015/06/PXD002049

#sim-xl
# test_loop.project("2017/05/PXD006574")
# test_loop.project("2015/02/PXD001677")

#missing file
# test_loop.project("2013/09/PXD000443")

#prob
# test_loop.project("2014/04/PXD000579")

print("mzId count:" + str(test_loop.mzId_count))


# need to defend against Connection reset by peer
# getting 160315_210_AN_10_f14.mgf
# Traceback (most recent call last):
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 399, in <module>
#     test_loop.year('2017')
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 90, in year
#     self.month(y + '/' + f)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 98, in month
#     self.project(ymp)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 115, in project
#     self.file(ymp, f)
#   File "/var/www/html/xiUI/xiSPEC_ms_parser/TestLoop.py", line 219, in file
#     open(self.temp_dir + peak_file, 'wb').write)
#   File "/usr/lib/python2.7/ftplib.py", line 414, in retrbinary
#     conn = self.transfercmd(cmd, rest)
#   File "/usr/lib/python2.7/ftplib.py", line 376, in transfercmd
#     return self.ntransfercmd(cmd, rest)[0]
#   File "/usr/lib/python2.7/ftplib.py", line 339, in ntransfercmd
#     resp = self.sendcmd(cmd)
#   File "/usr/lib/python2.7/ftplib.py", line 249, in sendcmd
#     return self.getresp()
#   File "/usr/lib/python2.7/ftplib.py", line 215, in getresp
#     resp = self.getmultiline()
#   File "/usr/lib/python2.7/ftplib.py", line 201, in getmultiline
#     line = self.getline()
#   File "/usr/lib/python2.7/ftplib.py", line 186, in getline
#     line = self.file.readline(self.maxline + 1)
#   File "/usr/lib/python2.7/socket.py", line 480, in readline
#     data = self._sock.recv(self._rbufsize)
# socket.error: [Errno 104] Connection reset by peer
