import csv
import importlib.util
import ipaddress
import logging
import re
import subprocess
from base64 import b64decode, b64encode
from builtins import ZeroDivisionError
from copy import copy
from csv import Error, Sniffer, reader
from enum import IntEnum
from pathlib import Path
from typing import List

import requests
from bs4 import BeautifulSoup

from .config import Config
from .contacts import Contacts
from .graph import Graph
from .whois import Whois

logger = logging.getLogger(__name__)

reIpWithPort = re.compile("((\d{1,3}\.){4})(\d+)")
reAnyIp = re.compile("\"?((\d{1,3}\.){3}(\d{1,3}))")
reFqdn = re.compile(
    "(?=^.{4,253}$)(^((?!-)[a-zA-Z0-9-]{1,63}(?<!-)\.)+[a-zA-Z]{2,63}$)")  # Xtoo long, infinite loop: ^(((([A-Za-z0-9]+){1,63}\.)|(([A-Za-z0-9]+(\-)+[A-Za-z0-9]+){1,63}\.))+){1,255}$
reUrl = re.compile('[htps]*://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+')


# reBase64 = re.compile('^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$')


def check_ip(ip):
    """ True, if IP is well formatted IPv4 or IPv6 """
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


class Checker:
    """ To not pollute the namespace, we put the methods here """

    @staticmethod
    def check_cidr(cidr):
        try:
            ipaddress.ip_interface(cidr)
            try:
                ipaddress.ip_address(cidr)
            except ValueError:  # "1.2.3.4" fail, "1.2.3.4/24" pass
                return True
        except ValueError:
            pass

    @staticmethod
    def is_base64(x):
        # there must be at least single letter, port number would be mistaken for base64 fields
        return base64decode(x) and re.search(r"[A-Za-z]", x)

    @staticmethod
    def check_wrong_url(wrong):
        s = wrong_url_2_url(wrong, make=False)
        return (not reUrl.match(wrong) and not reFqdn.match(wrong)) and (reUrl.match(s) or reFqdn.match(s))


def wrong_url_2_url(s, make=True):
    s = s.replace("hxxp", "http", 1).replace("[.]", ".").replace("[:]", ":")
    if make and not s.startswith("http"):
        s = "http://" + s
    return s


def any_ip_2_ip(s):
    m = reAnyIp.search(s)
    if m:
        return m.group(1)


def port_ip_2_ip(s):
    m = reIpWithPort.match(s)
    if m:
        return m.group(1).rstrip(".")


def base64decode(x):
    try:
        return b64decode(x).decode("UTF-8").replace("\n", "\\n")
    except (UnicodeDecodeError, ValueError):
        return None


class Web:
    """
    :return: self.get = [http status | error, shortened text, original html, redirects]
    """
    cache = {}
    store_html = True
    store_text = True
    headers = {}

    @classmethod
    def init(cls, fields=[]):
        if fields:
            cls.store_html = Types.html in [f.type for f in fields]
            cls.store_text = Types.text in [f.type for f in fields]
        else:
            cls.store_html = cls.store_text = True
        if Config.get("user_agent", "FIELDS"):
            cls.headers = {"User-Agent": Config.get("user_agent", "FIELDS")}

    def __init__(self, url):
        if url in self.cache:
            self.get = self.cache[url]
            return
        try:
            logger.info(f"Scrapping {url}...")
            response = requests.get(url, timeout=3, headers=self.headers)
        except IOError as e:
            self.get = str(e), None, None, None
        else:
            response.encoding = response.apparent_encoding  # https://stackoverflow.com/a/52615216/2036148
            if self.store_text:
                soup = BeautifulSoup(response.text, features="html.parser")
                [s.extract() for s in soup(["style", "script", "head"])]  # remove tags with low probability of content
                text = re.sub(r'\n\s*\n', '\n', soup.text)  # reduce multiple new lines to singles
                text = re.sub(r'[^\S\r\n][^\S\r\n]*[^\S\r\n]', ' ', text)  # reduce multiple spaces (not new lines) to singles
            else:
                text = None
            redirects = ""
            for res in response.history[1:]:
                redirects = f"REDIRECT {res.status_code} → {res.url}\n" + text
            self.get = response.status_code, text, response.text if self.store_html else None, redirects
        self.cache[url] = self.get


def nmap(val):
    logger.info(f"NMAPing {val}...")
    text = subprocess.run(["nmap", val], stdout=subprocess.PIPE).stdout.decode("utf-8")
    text = text[text.find("PORT"):]
    text = text[text.find("\n") + 1:]
    text = text[:text.find("\n\n")]
    return text


class TypeGroup(IntEnum):
    general = 1
    custom = 2
    whois = 3
    dns = 4
    nmap = 5
    web = 6

    def disable(self):
        for start, target in copy(methods):
            if start.group is self or target.group is self:
                del methods[start, target]
        for f in Types.get_guessable_types():
            if f.group is self:
                f.is_disabled = True


class Type:
    """
    A field type Convey is able to identify or compute
    """

    def __init__(self, name, group=TypeGroup.general, description=None, usual_names=[], identify_method=None, is_private=False,
                 from_message=None):
        """
        :param name: Key name
        :param description: Help text
        :param usual_names: Names this column usually has (ex: source_ip for an IP column). List of str, lowercase, no spaces.
        :param identify_method: Lambda used to identify a value may be of this field type
        :param is_private: User cannot add the field type (ex: whois, user can extend only netname which is accessed through it).
        """
        self.name = name
        self.group = group
        self.usual_names = usual_names
        self.identify_method = identify_method
        self.description = description
        self.is_private = is_private
        self.is_disabled = False  # disabled field cannot be added (and computed)
        self.from_message = from_message
        types.append(self)
        self.after = self.before = self
        self.is_plaintext_derivable = False

    def __eq__(self, other):
        if type(other) is str:
            return self.name == other
        return self.name == other.name

    def __lt__(self, other):
        if isinstance(other, str):
            return self.name < other
        return (self.group, self.name) < (other.group, other.name)

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"Type({self.name})"

    def doc(self):
        s = self.name
        if self.description:
            s += f" ({self.description})"
        if self.usual_names:
            s += f" usual names: " + ", ".join(self.usual_names)
        return s

    def __add__(self, other):  # sometimes we might get compared to string columns names from the CSV
        if isinstance(other, str):
            return self.name + " " + other
        return self.name + " " + other.name

    def __radd__(self, other):  # sometimes we might get compared to string columns names from the CSV
        if isinstance(other, str):
            return other + " " + self.name
        return self.other + " " + self.name

    def _init(self):
        """ Init self.after and self.before . """
        afters = [stop for start, stop in methods if start is self and methods[start, stop] is True]
        befores = [start for start, stop in methods if stop is self and methods[start, stop] is True]
        if len(afters) == 1:
            self.after = afters[0]
        elif len(afters):
            raise RuntimeWarning(f"Multiple 'afters' types defined for {self}: {afters}")
        if len(befores) == 1:
            self.before = befores[0]
        elif len(befores):
            raise RuntimeWarning(f"Multiple 'befores' types defined for {self}: {befores}")

        # check if this is plaintext derivable
        if self != Types.plaintext:
            self.is_plaintext_derivable = bool(graph.dijkstra(self, start=Types.plaintext))


types: List[Type] = []  # all field types


class Types:
    """
    Methods sourcing from private type should return a tuple, with the private type as the first
    Ex: (whois, asn): lambda x: (x, x.get[3])

    """

    whois = Type("whois", TypeGroup.whois, "ask whois servers", is_private=True)
    web = Type("web", TypeGroup.web, "scrape web contents", is_private=True)

    custom = Type("custom", TypeGroup.custom, from_message="from a method in your .py file")
    code = Type("code", TypeGroup.custom, from_message="from a code you write")
    reg = Type("reg", TypeGroup.custom, from_message="from a regular expression")
    #reg_s = Type("reg_s", TypeGroup.custom, from_message="substitution from a regular expression")
    #reg_m = Type("reg_m", TypeGroup.custom, from_message="match from a regular expression")
    netname = Type("netname", TypeGroup.whois)
    country = Type("country", TypeGroup.whois)
    abusemail = Type("abusemail", TypeGroup.whois)
    prefix = Type("prefix", TypeGroup.whois)
    csirt_contact = Type("csirt_contact", TypeGroup.whois)
    incident_contact = Type("incident_contact", TypeGroup.whois)
    decoded_text = Type("decoded_text", TypeGroup.general)
    text = Type("text", TypeGroup.web)
    http_status = Type("http_status", TypeGroup.web)
    html = Type("html", TypeGroup.web)
    redirects = Type("redirects", TypeGroup.web)
    ports = Type("ports", TypeGroup.nmap)

    ip = Type("ip", TypeGroup.general, "valid IP address", ["ip", "sourceipaddress", "ipaddress", "source"], check_ip)
    cidr = Type("cidr", TypeGroup.general, "CIDR 127.0.0.1/32", ["cidr"], Checker.check_cidr)
    port_ip = Type("portIP", TypeGroup.general, "IP in the form 1.2.3.4.port", [], reIpWithPort.match)
    any_ip = Type("anyIP", TypeGroup.general, "IP in the form 'any text 1.2.3.4 any text'", [],
                  lambda x: reAnyIp.search(x) and not check_ip(x))
    hostname = Type("hostname", TypeGroup.general, "2nd or 3rd domain name", ["fqdn", "hostname", "domain"], reFqdn.match)
    url = Type("url", TypeGroup.general, "URL starting with http/https", ["url", "uri", "location"],
               lambda s: reUrl.match(s) and "[.]" not in s)  # input "example[.]com" would be admitted as a valid URL)
    asn = Type("asn", TypeGroup.whois, "AS Number", ["as", "asn", "asnumber"],
               lambda x: re.search('AS\d+', x) is not None)
    base64 = Type("base64", TypeGroup.general, "Text encoded with Base64", ["base64"], Checker.is_base64)
    base64_encoded = Type("base64_encoded", TypeGroup.general)
    wrong_url = Type("wrongURL", TypeGroup.general, "Deactivated URL", [], Checker.check_wrong_url)
    plaintext = Type("plaintext", TypeGroup.general, "Plain text", ["plaintext", "text"], lambda x: False)

    @staticmethod
    def get_computable_types():
        """ List of all suitable fields that we may compute from a suitable output """
        return sorted({target_type for _, target_type in methods.keys() if not (target_type.is_private or target_type.is_disabled)})

    @staticmethod
    def get_guessable_types() -> List[Type]:
        """ these field types can be guessed from a string """
        return sorted([t for t in types if not t.is_disabled and (t.identify_method or t.usual_names)])

    @staticmethod
    def get_uml():
        """ Return DOT UML source code of types and methods"""
        l = ['digraph { ']
        l.append('label="Convey field types (dashed = identifiable automatically, circled = IO actions)"')
        for f in types:
            label = [f.name]
            if f.description:
                label.append(f.description)
            if f.usual_names:
                label.append("usual names: " + ", ".join(f.usual_names))
            s = "\n".join(label)
            l.append(f'{f.name} [label="{s}"]')
            if f in Types.get_guessable_types():
                l.append(f'{f.name} [style=dashed]')
            if f.is_private:
                l.append(f'{f.name} [shape=circled]')

        for k, v in methods:
            l.append(f"{k} -> {v};")
        l.append("}")
        return "\n".join(l)

    @staticmethod
    def _get_methods():
        """  these are known methods to make a field from another field
            Note that Whois only produces tuple to be fetchable to stats in Processor, the others are rather strings.

            Method ~ None: TypeGroup.custom fields should have the method be None.
            Method ~ True: The user should not be offered to compute from the field to another.
                However if they already got the first type, it is the same as if they had the other.
                Example: XXX source_ip,
                Example: (base64 → base64_encoded) We do not want to offer the conversion plaintext → base64 → decoded_text,
                    however we allow conversion base64 → (invisible base64_encoded) → decoded_text
        """
        t = Types
        return {(t.any_ip, t.ip): any_ip_2_ip,
                # any IP: "91.222.204.175 93.171.205.34" -> "91.222.204.175" OR '"1.2.3.4"' -> 1.2.3.4
                (t.port_ip, t.ip): port_ip_2_ip,
                # portIP: IP written with a port 91.222.204.175.23 -> 91.222.204.175
                (t.url, t.hostname): Whois.url2hostname,
                (t.hostname, t.ip): Whois.hostname2ip,
                (t.url, t.ip): Whois.url2ip,
                (t.ip, t.whois): Whois,
                (t.cidr, t.ip): lambda x: str(ipaddress.ip_interface(x).ip),
                (t.whois, t.prefix): lambda x: (x, str(x.get[0])),
                (t.whois, t.asn): lambda x: (x, x.get[3]),
                (t.whois, t.abusemail): lambda x: (x, x.get[6]),
                (t.whois, t.country): lambda x: (x, x.get[5]),
                (t.whois, t.netname): lambda x: (x, x.get[4]),
                (t.whois, t.csirt_contact): lambda x: (
                    x, Contacts.csirtmails[x.get[5]] if x.get[5] in Contacts.csirtmails else "-"),
                # returns tuple (local|country_code, whois-mail|abuse-contact)
                (t.whois, t.incident_contact): lambda x: (x, x.get[2]),
                # (t.base64, t.base64_encoded): True,
                # (t.base64_encoded, t.decoded_text): base64decode,
                (t.base64, t.plaintext): base64decode,
                (t.plaintext, t.base64): lambda x: b64encode(x.encode("UTF-8")).decode("UTF-8"),
                (t.plaintext, t.custom): None,
                (t.plaintext, t.code): None,
                (t.plaintext, t.reg): None,
                # (t.reg, t.reg_s): None,
                # (t.reg, t.reg_m): None,
                (t.wrong_url, t.url): wrong_url_2_url,
                (t.hostname, t.url): lambda x: "http://" + x,
                (t.ip, t.url): lambda x: "http://" + x,
                (t.url, t.web): Web,
                (t.web, t.http_status): lambda x: x.get[0],
                (t.web, t.text): lambda x: x.get[1],
                (t.web, t.html): lambda x: x.get[2],
                (t.web, t.redirects): lambda x: x.get[3],
                (t.hostname, t.ports): nmap,
                (t.ip, t.ports): nmap
                # (t.abusemail, t.email): True
                # (t.email, t.hostname): ...
                # (t.email, check legit mailbox)
                # (t.source_ip, t.ip): True
                # (t.phone, t.country): True
                # ("hostname", "spf"):
                # (t.country, t.country_name):
                # (t.hostname, t.tld)
                # (t.tld, t.country)
                # (t.prefix, t.cidr)
                # XX dns dig
                # XX url decode
                # XX timestamp
                }


methods = Types._get_methods()
graph = Graph([t for t in types if t.is_private])  # methods converting a field type to another
[graph.add_edge(to, from_) for to, from_ in methods if methods[to, from_] is not True]
[t._init() for t in types]


class Identifier:

    def __init__(self, csv):
        """ import custom methods from files """
        for path in (x.strip() for x in Config.get("custom_fields_modules", "FIELDS", get=str).split(",")):
            try:
                module = self.get_module_from_path(path)
                if module:
                    for method in (x for x in dir(module) if not x.startswith("_")):
                        methods[(Types.plaintext, method)] = getattr(module, method)
                        logger.info("Successfully added method {method} from module {path}")
            except Exception as e:
                s = "Can't import custom file from path: {}".format(path)
                input(s + ". Press any key...")
                logger.warning(s)

        self.csv = csv
        self.graph = None

    def get_methods_from(self, target, start, custom):
        """
        Returns the nested lambda list that'll receive a value from start field and should produce value in target field.
        :param target: field type name
        :param start: field type name
        :param custom: If target is a 'custom' field type, we'll receive a tuple (module path, method name).
        :return: lambda[]
        """

        def custom_code(e):
            def method(x):
                l = locals()
                try:
                    exec(compile(e, '', 'exec'), l)
                except Exception as exception:
                    code = "\n  ".join(e.split("\n"))
                    logger.error(f"Statement failed with {exception}.\n  x = '{x}'; {code}")
                    if not Config.error_caught():  # XX ipdb cant be quit with q here
                        input("We consider 'x' unchanged...")
                    return x
                x = l["x"]
                return x

            return method

        def regex(search, replace=None):
            search = re.compile(search)

            def method(s):
                match = search.search(s)
                # print(match) XXX
                # print("Groups:", match.groups())
                # print("Nula:", match.group(0))

                if not match:
                    return ""
                groups = match.groups()
                if not replace:
                    if not groups:
                        return match.group(0)
                    return match.group(1)
                try:
                    return replace.format(match.group(0), *[g for g in groups])
                except IndexError:
                    logger.error(f"RegExp failed: `{replace}` cannot be used to replace `{s}` with `{search}`")
                    if not Config.error_caught():
                        input("We consider string unmatched...")
                    return ""

            return method

        if target.group == TypeGroup.custom:
            if target == Types.custom:
                return [getattr(self.get_module_from_path(custom[0]), custom[1])]
            elif target == Types.code:
                return [custom_code(custom)]
            elif target == Types.reg:
                return [regex(*custom)]  # custom is in the form (search, replace)
            else:
                raise ValueError(f"Unknown type {target}")
        lambdas = []  # list of lambdas to calculate new field
        path = graph.dijkstra(target, start=start)  # list of method-names to calculate new fields
        for i in range(len(path) - 1):
            lambda_ = methods[path[i], path[i + 1]]
            if lambda_ is True:  # the field is invisible, see help text for Types
                continue
            lambdas.append(lambda_)
        return lambdas

    @staticmethod
    def get_sample(source_file):
        sample = []
        first_line = ""
        with open(source_file, 'r') as csv_file:
            for i, row in enumerate(csv_file):
                if i == 0:
                    first_line = row
                sample.append(row)
                if i == 8:  # sniffer needs 7+ lines to determine dialect, not only 3 (/mnt/csirt-rook/2015/06_08_Ramnit/zdroj), I dont know why
                    break
        return first_line.strip(), sample
        # csvfile.seek(0)
        # csvfile.close()

    @staticmethod
    def get_module_from_path(path):
        if not Path(path).is_file():
            return False
        spec = importlib.util.spec_from_file_location("", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def guess_dialect(sample):
        sniffer = Sniffer()
        sample_text = "".join(sample)
        try:
            dialect = sniffer.sniff(sample_text)
            has_header = sniffer.has_header(sample_text)
        except Error:  # delimiter failed – maybe there is an empty column: "89.187.1.81,06-05-2016,,CZ,botnet drone"
            if sample_text.strip() == "":
                print("The file seems empty")
                quit()
            has_header = False  # lets just guess the value
            try:
                s = sample[1]  # we dont take header (there is no empty column for sure)
            except IndexError:  # there is a single line in the file
                s = sample[0]
            delimiter = ""
            for dl in (",", ";", "|"):  # lets suppose the doubled sign is delimiter
                if s.find(dl + dl) > -1:
                    delimiter = dl
                    break
            if not delimiter:  # try find anything that resembles to a delimiter
                for dl in (",", ";", "|"):
                    if s.find(dl) > -1:
                        delimiter = dl
                        break
            dialect = csv.unix_dialect
            if delimiter:
                dialect.delimiter = delimiter
        if not dialect.escapechar:
            dialect.escapechar = '\\'
        # dialect.quoting = 3
        dialect.doublequote = True

        if dialect.delimiter == "." and "," not in sample_text:
            # let's propose common use case (bare list of IP addresses) over a strange use case with "." delimiting
            dialect.delimiter = ","
        if len(sample) == 1:
            # there is single line in sample = in the input, so this is definitely not a header
            has_header = False
        return dialect, has_header

    def init(self, quiet=False):
        """
        Identify self.csv.fields got in __init__
        Sets them possible types (sorted, higher score mean bigger probability that the field is of that type)
        :type quiet: bool If True, we do not raise exception when sample cannot be processed.
                            Ex: We attempt consider user input "1,2,3" as single field which is not, we silently return False
        """
        samples = [[] for _ in self.csv.fields]
        if len(self.csv.sample) == 1:  # we have too few values, we have to use them
            s = self.csv.sample[:1]
        else:  # we have many values and the first one could be header, let's omit it
            s = self.csv.sample[1:]

        for row in reader(s, dialect=self.csv.dialect):
            for i, val in enumerate(row):
                try:
                    samples[i].append(val)
                except IndexError:
                    if not quiet:
                        print("It seems rows have different lengths. Cannot help you with column identifying.")
                        print("Fields row: " + str([(i, str(f)) for i, f in enumerate(self.csv.fields)]))
                        print("Current row: " + str(list(enumerate(row))))
                        if not Config.error_caught():
                            input("\n... Press any key to continue.")
                    return False

        for i, field in enumerate(self.csv.fields):
            possible_types = {}
            for type_ in Types.get_guessable_types():
                score = 0
                # print("Guess:", key, names, checkFn)
                # guess field type by name
                if self.csv.has_header:
                    s = str(field).replace(" ", "").replace("'", "").replace('"', "").lower()
                    for n in type_.usual_names:
                        if s in n or n in s:
                            # print("HEADER match", field, names)
                            score += 1
                            break
                # else:
                # guess field type by few values
                hits = 0
                for val in samples[i]:
                    if type_.identify_method(val):
                        # print("Match")
                        hits += 1
                try:
                    percent = hits / len(samples[i])
                except ZeroDivisionError:
                    percent = 0
                if percent == 0:
                    continue
                elif percent > 0.6:
                    # print("Function match", field, checkFn)
                    score += 1
                    if percent > 0.8:
                        score += 1

                possible_types[type_] = score
                # print("hits", hits)
            if possible_types:  # sort by biggest score - biggest probability the column is of this type
                field.possible_types = {k: v for k, v in sorted(possible_types.items(), key=lambda k: k[1], reverse=True)}
        return True

    def get_fitting_type(self, source_field_i, target_field, try_plaintext=False):
        """ Loops all types the field could be and return the type best suited method for compute new field. """
        _min = 999
        fitting_type = None
        possible_fields = list(self.csv.fields[source_field_i].possible_types)
        if try_plaintext:  # try plaintext field as the last one
            possible_fields.append(Types.plaintext)
        dijkstra = graph.dijkstra(target_field)  # get all fields that new_field is computable from
        for _type in possible_fields:
            # loop all the types the field could be, loop from the FieldType we think the source_col correspond the most
            # a column may have multiple types (url, hostname), use the best
            if _type not in dijkstra:
                continue
            i = dijkstra[_type]
            if i < _min:
                _min, fitting_type = i, _type
        return fitting_type

    def get_fitting_source_i(self, new_field, try_hard=False):
        """ Get list of source_i that may be of such a field type that new_field would be computed effectively.
            Note there is no fitting column for TypeGroup.custom, if you try_hard, you receive first column as a plaintext.
        """
        possible_cols = {}
        if new_field.group != TypeGroup.custom:
            valid_types = graph.dijkstra(new_field)
            for val in valid_types:  # loop from the best suited type
                for i, f in enumerate(self.csv.fields):  # loop from the column we are most sure with its field type
                    if val in f.possible_types:
                        possible_cols[i] = f.possible_types[val]
                        break
        if not possible_cols and try_hard and new_field.is_plaintext_derivable:
            # because any plaintext would do (and no plaintext-only type has been found), take the first column
            possible_cols = [0]
        return list(possible_cols)

    def get_fitting_source(self, new_field: Type, column_or_source, source_type_candidate):
        """
        For a new field, we need source column and its field type to compute new field from.
        :param new_field: str of Type
        :param column_or_source: [int|existing name|field name|field usual names]
        :param source_type_candidate: [field name|field usual names]
        :return: (source_field, source_type) or exit.
        """
        source_col_i = None
        source_type = None
        custom = None
        if column_or_source:  # determine COLUMN
            source_col_i = self.get_column_i(column_or_source)
            if source_col_i is None:
                if source_type_candidate:
                    print("Invalid field", source_type_candidate, ", already having defined field by " + column_or_source)
                    quit()
                else:  # this was not COLUMN but SOURCE_TYPE, COLUMN remains empty
                    source_type_candidate = column_or_source
        else:  # get a column whose field could be fitting for that new_field
            try:
                source_col_i = self.get_fitting_source_i(new_field, True)[0]
            except IndexError:
                pass
        if source_type_candidate:  # determine SOURCE_TYPE
            if new_field.group == TypeGroup.custom:
                custom = source_type_candidate
                source_type = Types.plaintext
            else:
                source_t = source_type_candidate.lower().replace(" ", "")  # make it seem like a usual field name
                possible = None
                for t in types:
                    if source_t == t:  # exact field name
                        source_type = t
                        break
                    if source_t in t.usual_names:  # usual field name
                        possible = t
                else:
                    if possible:
                        source_type = possible
                if not source_type:
                    print(f"Cannot determine new field from {source_t}")
                    quit()
        if source_col_i is not None and not source_type:
            source_type = self.get_fitting_type(source_col_i, new_field, try_plaintext=True)
            if not source_type:
                print(f"We could not identify a method how to make '{new_field}' from '{self.csv.fields[source_col_i]}'")
                quit()
        if source_type and source_col_i is None:
            # searching for a fitting type amongst existing columns
            # for col in self.
            possibles = {}  # [source col i] = score (bigger is better)
            for i, t in enumerate(self.csv.fields):
                if source_type in t.possible_types:
                    possibles[i] = t.possible_types[source_type]

            try:
                source_col_i = sorted(possibles, key=possibles.get, reverse=True)[0]
            except IndexError:
                print(f"No suitable column of type '{source_type}' found to make field '{new_field}'")
                quit()
        if not source_type or source_col_i is None:
            print(f"No suitable column found for field '{new_field}'")
            quit()
        return self.csv.fields[source_col_i], source_type, custom

    def get_column_i(self, column):
        """
        Useful for parsing user input COLUMN from the CLI args.
        :type column: object Either the order of the column or an exact column name
        :rtype: int Either column_i or None if not found.
        """
        source_col_i = None
        if column.isdigit():  # number of column
            source_col_i = int(column) - 1
        elif column in self.csv.first_line_fields:  # exact column name
            source_col_i = self.csv.first_line_fields.index(column)
        return source_col_i
