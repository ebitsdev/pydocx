from abc import abstractmethod, ABCMeta
try:
    from collections import OrderedDict
except ImportError:  # Python 2.6
    from ordereddict import OrderedDict
import zipfile
import logging
from contextlib import contextmanager
import xml.etree.ElementTree as ElementTree
from xml.etree.ElementTree import _ElementInterface
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("NewParser")


# http://openxmldeveloper.org/discussions/formats/f/15/p/396/933.aspx
EMUS_PER_PIXEL = 9525


def remove_namespaces(document):  # remove namespaces
    root = ElementTree.fromstring(document)
    for child in el_iter(root):
        child.tag = child.tag.split("}")[1]
        child.attrib = dict(
            (k.split("}")[-1], v)
            for k, v in child.attrib.items()
        )
    return ElementTree.tostring(root)

# Add some helper functions to Element to make it slightly more readable


# determine if current element has a child. stop at first child.
def has_child(self, tag):
    return True if self.find(tag) is not None else False


# determine if there is a child ahead in the element tree.
def has_child_deep(self, tag):
                              # get child. stop at first child.
    return True if self.find('.//' + tag) is not None else False


# find the first occurrence of a tag beneath the current element
def find_first(self, tag):
    return self.find('.//' + tag)


def find_all(self, tag):  # find all occurrences of a tag
    return self.findall('.//' + tag)


def el_iter(el):  # go through all elements
    try:
        return el.iter()
    except AttributeError:
        return el.findall('.//*')


def find_parent_by_tag(self, tag):
    el = self
    while el.parent:
        el = el.parent
        if el.tag == tag:
            return el
    return None


#make all of these attributes of _ElementInterface
setattr(_ElementInterface, 'has_child', has_child)
setattr(_ElementInterface, 'has_child_deep', has_child_deep)
setattr(_ElementInterface, 'find_first', find_first)
setattr(_ElementInterface, 'find_all', find_all)
setattr(_ElementInterface, 'find_parent_by_tag', find_parent_by_tag)
setattr(_ElementInterface, 'parent', None)
setattr(_ElementInterface, 'is_first_list_item', False)
setattr(_ElementInterface, 'is_last_list_item', False)
setattr(_ElementInterface, 'next', None)
setattr(_ElementInterface, 'previous', None)

# End helpers


@contextmanager
def ZipFile(path):  # This is not needed in python 3.2+
    f = zipfile.ZipFile(path)
    yield f
    f.close()


@contextmanager
def create_list():
    yield ['<ol>', '</ol>']


class DocxParser:
    __metaclass__ = ABCMeta

    def _build_data(self, path, *args, **kwargs):
        with ZipFile(path) as f:
            self.document_text = f.read('word/document.xml')
            try:  # Only present if there are lists
                self.numbering_text = f.read('word/numbering.xml')
            except KeyError:
                self.numbering_text = None
            try:  # Only present if there are comments
                self.comment_text = f.read('word/comments.xml')
            except KeyError:
                self.comment_text = None
            self.relationship_text = f.read('word/_rels/document.xml.rels')

        self.root = ElementTree.fromstring(
            remove_namespaces(self.document_text),  # remove the namespaces
        )
        self.numbering_root = None
        if self.numbering_text:
            self.numbering_root = ElementTree.fromstring(
                remove_namespaces(self.numbering_text),
            )
        self.comment_root = None
        if self.comment_text:
            self.comment_root = ElementTree.fromstring(
                remove_namespaces(self.comment_text),
            )

    def _parse_rels_root(self):
        tree = ElementTree.fromstring(self.relationship_text)
        rels_dict = {}
        for el in tree:
            rId = el.get('Id')
            target = el.get('Target')
            rels_dict[rId] = target
        return rels_dict

    def __init__(self, *args, **kwargs):
        self._parsed = ''
        self.in_list = False

        self._build_data(*args, **kwargs)

        def add_parent(el):  # if a parent, make that an attribute
            for child in el.getchildren():
                setattr(child, 'parent', el)
                add_parent(child)

        add_parent(self.root)  # create the parent attributes

        #all blank when we init
        self.comment_store = None
        self.elements = []
        self.visited = []
        self.rels_dict = self._parse_rels_root()
        self.parse_begin(self.root)  # begin to parse

    def parse_begin(self, el):
        # Find the first and last li elements
        list_elements = el.find_all('numId')
        num_ids = set([i.attrib['val'] for i in list_elements])
        list_elements = el.find_all('ilvl')
        ilvls = set([i.attrib['val'] for i in list_elements])
        elements = [i.find_parent_by_tag('p') for i in list_elements]
        for num_id in num_ids:
            for ilvl in ilvls:
                filtered_list_elements = [
                    i for i in elements
                    if (
                        i.find_first('numId').attrib['val'] == num_id and
                        i.find_first('ilvl').attrib['val'] == ilvl)
                ]
                if not filtered_list_elements:
                    continue
                first_el = filtered_list_elements[0]
                first_el.is_first_list_item = True
                last_el = filtered_list_elements[-1]
                last_el.is_last_list_item = True

        body = el.find_first('body')
        children = [
            child for child in body.getchildren()
            if child.tag in ['p', 'tbl']
        ]
        for i in range(len(children)):
            try:
                if children[i - 1]:
                    children[i].previous = children[i - 1]
                if children[i + 1]:
                    children[i].next = children[i + 1]
            except IndexError:
                pass

        #self._parsed += self.parse_lists(el)  # start out wth lists
        self._parsed += self.parse(el)

### parse table function and is_table flag
    def parse_lists(self, el):
        parsed = ''
        body = el.find_first('body')
        children = [
            child for child in body.getchildren()
            if child.tag in ['p', 'tbl']
        ]

        # Find the first and last li elements
        list_elements = el.find_all('numId')
        num_ids = set([i.attrib['val'] for i in list_elements])
        for num_id in num_ids:
            filtered_list_elements = [
                i for i in list_elements
                if i.attrib['val'] == num_id
            ]
            filtered_list_elements[0].is_first_list_item = True
            filtered_list_elements[-1].is_last_list_item = True

        p_list = children  # p_list is now children
        list_started = False  # list has not started yet
        list_type = ''
        list_chunks = []
        index_start = 0
        index_end = 1
        # enumerate p_list so we have a tuple of # and element
        for i, el in enumerate(p_list):
            # if list hasn't started and the element has a child
            if not list_started and el.has_child_deep('ilvl'):
                list_started = True  # list has child
                list_type = self.get_list_style(  # get the type of list
                    el.find_first('numId').attrib['val'],
                )
                # append the current and next to list_chunks
                list_chunks.append(p_list[index_start:index_end])
                index_start = i
                index_end = i+1
            elif (
                    list_started and
                    el.has_child_deep('ilvl') and
                    # if the list has started and the list type has changed,
                    # change the list type
                    not list_type == self.get_list_style(
                        el.find_first('numId').attrib['val']
                    )):
                list_type = self.get_list_style(
                    el.find_first('numId').attrib['val'],
                )
                list_started = True
                list_chunks.append(p_list[index_start:index_end])
                index_start = i
                index_end = i+1
            elif list_started and not el.has_child_deep('ilvl'):
                # if there are no more children that are part of a list, list
                # start is false
                list_started = False
                list_chunks.append(p_list[index_start:index_end])
                index_start = i
                index_end = i+1
            else:
                index_end = i+1
        list_chunks.append(p_list[index_start:index_end])
        chunk_info = {}
        lst_info = {}
        # if there is a list, group all the numIds together and sort, else just
        # have a list of the relevant chunks!
        for i, chunk in enumerate(list_chunks):
            if chunk[0].has_child_deep('ilvl'):
                numId = chunk[0].find_first('numId').attrib['val']
                lst_info[numId] = chunk
                lst_info = OrderedDict(lst_info.items())
                chunk_info[i] = lst_info

            else:
                chunk_info[i] = chunk
        chunk_info = OrderedDict(sorted(chunk_info.items()))
        for i, chunk in chunk_info.iteritems():
            chunk_parsed = ''
            if type(chunk) is not OrderedDict:
                for el in chunk:
                    chunk_parsed += self.parse(el)
                parsed += chunk_parsed
            else:
                for chunk in chunk.itervalues():
                    chunk_parsed = ''
                    for el in chunk:
                        chunk_parsed += self.parse(el)
                    lst_style = self.get_list_style(
                        chunk[0].find_first('numId').attrib['val'],
                    )
                    # check if blank
                    if lst_style['val'] == 'bullet' and chunk_parsed != '':
                        parsed += self.unordered_list(chunk_parsed)
                    elif lst_style['val'] and chunk_parsed != '':
                        parsed += self.ordered_list(
                            chunk_parsed,
                            lst_style['val'],
                        )
            if chunk[0].has_child_deep('br'):
                parsed += self.page_break()
        return parsed

    def parse(self, el):
        if el in self.visited:
            return ''
        self.visited.append(el)
        parsed = ''

        for child in el:
            # recursive. so you can get all the way to the bottom
            parsed += self.parse(child)

        if el.is_first_list_item:
            return self.parse_list(el, parsed)
        if el.tag == 'br' and el.attrib.get('type') == 'page':
            #TODO figure out what parsed is getting overwritten
            return self.page_break()
        # Do not do the tr or tc a second time
        if el.tag == 'tbl':
            return self.table(parsed)
        elif el.tag == 'tr':  # table rows
            return self.table_row(parsed)
        elif el.tag == 'tc':  # table cells
            #self.elements.append(el)
            return self.table_cell(parsed)
        if el.tag == 'r' and el not in self.elements:
            self.elements.append(el)
            return self.parse_r(el)  # parse the run
        elif el.tag == 'p':
            if el.parent.tag == 'tc':
                return parsed  # return text in the table cell
            # parse p. parse p will return a list element or a paragraph
            return self.parse_p(el, parsed)
        elif el.tag == 'ins':
            return self.insertion(parsed, '', '')
        elif el.tag == 'hyperlink':
            return self.parse_hyperlink(el, parsed)
        else:
            return parsed

    def parse_list(self, el, text):
        parsed = self.parse_p(el, text)
        num_id = el.find_first('numId').attrib['val']
        next_el = el.next
        while next_el and not next_el.is_last_list_item:
            current_num_id = next_el.find_first('numId')
            #print current_num_id.attrib['val']
            if current_num_id is not None:
                if current_num_id.attrib['val'] != num_id:
                    break
            parsed += self.parse(next_el)
            next_el = next_el.next
        if next_el is not None:
            last_num_id = next_el.find_first('numId')
            if last_num_id is not None and last_num_id.attrib['val'] == num_id:
                parsed += self.parse(next_el)

        lst_style = self.get_list_style(
            el.find_first('numId').attrib['val'],
        )
        # check if blank
        if lst_style['val'] == 'bullet' and parsed != '':
            return self.unordered_list(parsed)
        elif lst_style['val'] and parsed != '':
            return self.ordered_list(
                parsed,
                lst_style['val'],
            )

    def parse_p(self, el, text):
        # still need to go thru empty lists!
        if text == '' and not self.in_list:
            return ''
        parsed = text
        if el.has_child_deep('ilvl'):
            parsed = self.list_element(parsed)  # if list wrap in li tags
        elif el.parent not in self.elements:
            parsed = self.paragraph(parsed)  # if paragraph wrap in p tags
        return parsed

    def parse_hyperlink(self, el, text):
        rId = el.get('id')
        href = self.rels_dict.get(rId)
        if not href:
            return text
        href = self.escape(href)
        return self.hyperlink(text, href)

    def _get_image_id(self, el):
        # Drawings
        blip = el.find_first('blip')
        if blip is not None:
            # On drawing tags the id is actually whatever is returned from the
            # embed attribute on the blip tag. Thanks a lot Microsoft.
            return blip.get('embed')
        # Picts
        imagedata = el.find_first('imagedata')
        if imagedata is not None:
            return imagedata.get('id')

    def _convert_image_size(self, size):
        return size / EMUS_PER_PIXEL

    def _get_image_size(self, el):
        """
        If we can't find a height or width, return 0 for whichever is not
        found, then rely on the `image` handler to strip those attributes. This
        functionality can change once we integrate PIL.
        """
        sizes = el.find_first('ext')
        if sizes is not None:
            x = self._convert_image_size(int(sizes.get('cx')))
            y = self._convert_image_size(int(sizes.get('cy')))
            return (
                '%dpx' % x,
                '%dpx' % y,
            )
        shape = el.find_first('shape')
        if shape is not None:
            # If either of these are not set, rely on the method `image` to not
            # use either of them.
            x = 0
            y = 0
            styles = shape.get('style').split(';')
            for s in styles:
                if s.startswith('height:'):
                    y = s.split(':')[1]
                if s.startswith('width:'):
                    x = s.split(':')[1]
            return x, y
        return 0, 0

    def parse_image(self, el):
        x, y = self._get_image_size(el)
        rId = self._get_image_id(el)
        src = self.rels_dict.get(rId)
        if not src:
            return ''
        src = self.escape(src)
        return self.image(src, x, y)

    def _is_style_on(self, el):
        """
        For b, i, u (bold, italics, and underline) merely having the tag is not
        sufficient. You need to check to make sure it is not set to "false" as
        well.
        """
        return el.get('val') != 'false'

    def parse_r(self, el):  # parse the running text
        is_deleted = False
        text = ''
        for element in el:
            if element.tag == 't':
                text += self.escape(el.find('t').text)
            elif element.tag == 'delText':  # get the deleted text
                text += self.escape(el.find('delText').text)
                is_deleted = True
            elif element.tag in ('pict', 'drawing'):
                text += self.parse_image(element)
        if text:
            rpr = el.find('rPr')
            if rpr is not None:
                fns = []
                if rpr.has_child('b'):  # text styling
                    if self._is_style_on(rpr.find('b')):
                        fns.append(self.bold)
                if rpr.has_child('i'):
                    if self._is_style_on(rpr.find('i')):
                        fns.append(self.italics)
                if rpr.has_child('u'):
                    if self._is_style_on(rpr.find('u')):
                        fns.append(self.underline)
                for fn in fns:
                    text = fn(text)
            ppr = el.parent.find('pPr')
            if ppr is not None:
                jc = ppr.find('jc')
                if jc is not None:  # text alignments
                    if jc.attrib['val'] == 'right':
                        text = self.right_justify(text)
                    if jc.attrib['val'] == 'center':
                        text = self.center_justify(text)
                ind = ppr.find('ind')
                if ind is not None:
                    right = None
                    left = None
                    firstLine = None
                    if 'right' in ind.attrib:
                        right = ind.attrib['right']
                        right = int(right)/20
                        right = str(right)
                    if 'left' in ind.attrib:
                        left = ind.attrib['left']
                        left = int(left)/20
                        left = str(left)
                    if 'firstLine' in ind.attrib:
                        firstLine = ind.attrib['firstLine']
                        firstLine = int(firstLine)/20
                        firstLine = str(firstLine)
                    text = self.indent(text, right, left, firstLine)
            if is_deleted:
                text = self.deletion(text, '', '')
            return text
        else:
            return ''

    def get_list_style(self, numval):
        ids = self.numbering_root.find_all('num')
        for _id in ids:
            if _id.attrib['numId'] == numval:
                abstractid = _id.find('abstractNumId')
                abstractid = abstractid.attrib['val']
                style_information = self.numbering_root.find_all(
                    'abstractNum',
                )
                for info in style_information:
                    if info.attrib['abstractNumId'] == abstractid:
                        for i in el_iter(info):
                            if i.find('numFmt') is not None:
                                return i.find('numFmt').attrib

    def get_comments(self, doc_id):
        if self.comment_root is None:
            return ''
        if self.comment_store is not None:
            return self.comment_store[doc_id]
        ids_and_info = {}
        ids = self.comment_root.find_all('comment')
        for _id in ids:
            ids_and_info[_id.attrib['id']] = {
                "author": _id.attrib['author'],
                "date": _id.attrib['date'],
                "text": _id.find_all('t')[0].text,
            }
        self.comment_store = ids_and_info
        return self.comment_store[doc_id]

    @property
    def parsed(self):
        return self._parsed

    @property
    def escape(self, text):
        return text

    @abstractmethod
    def linebreak(self):
        return ''

    @abstractmethod
    def paragraph(self, text):
        return text

    @abstractmethod
    def insertion(self, text, author, date):
        return text

    @abstractmethod
    def hyperlink(self, text, href):
        return text

    @abstractmethod
    def image_handler(self, path):
        return path

    @abstractmethod
    def image(self, path, x, y):
        return self.image_handler(path)

    @abstractmethod
    def deletion(self, text, author, date):
        return text

    @abstractmethod
    def bold(self, text):
        return text

    @abstractmethod
    def italics(self, text):
        return text

    @abstractmethod
    def underline(self, text):
        return text

    @abstractmethod
    def tab(self):
        return True

    @abstractmethod
    def ordered_list(self, text):
        return text

    @abstractmethod
    def unordered_list(self, text):
        return text

    @abstractmethod
    def list_element(self, text):
        return text

    @abstractmethod
    def table(self, text):
        return text

    @abstractmethod
    def table_row(self, text):
        return text

    @abstractmethod
    def table_cell(self, text):
        return text

    @abstractmethod
    def page_break(self):
        return True

    @abstractmethod
    def right_justify(self, text):
        return text

    @abstractmethod
    def center_justify(self, text):
        return text

    @abstractmethod
    def indent(self, text, left=None, right=None, firstLine=None):
        return text

    #TODO JUSTIFIED JUSTIFIED TEXT
