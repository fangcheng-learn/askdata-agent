"""Small OOXML table export; preserves raw values and never emits formulas."""
from io import BytesIO
from math import isfinite
from xml.sax.saxutils import escape
from zipfile import ZipFile,ZIP_DEFLATED

def workbook_bytes(columns, rows):
    def column_name(i):
        out=''
        while i:i,remainder=divmod(i-1,26);out=chr(65+remainder)+out
        return out
    xml=[]
    for r,values in enumerate([columns,*[[row.get(c) for c in columns] for row in rows]],1):
        cells=[]
        for c,value in enumerate(values,1):
            ref=column_name(c)+str(r)
            if isinstance(value,(float,int)) and not isinstance(value,bool) and isfinite(value):cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                text=escape('' if value is None else str(value));text=''.join(c for c in text if ord(c)>=32 or c in '\t\n\r')
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        xml.append(f'<row r="{r}">'+''.join(cells)+'</row>')
    stream=BytesIO()
    with ZipFile(stream,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="分析结果" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml)+'</sheetData></worksheet>')
    return stream.getvalue()
