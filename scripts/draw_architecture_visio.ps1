param(
    [string]$PageName = "Bank OCR Architecture"
)

$ErrorActionPreference = "Stop"

function Get-VisioApplication {
    try {
        return [Runtime.InteropServices.Marshal]::GetActiveObject("Visio.Application")
    }
    catch {
        $visio = New-Object -ComObject "Visio.Application"
        $visio.Visible = $true
        return $visio
    }
}

function Set-CellFormula {
    param(
        [object]$Shape,
        [string]$Cell,
        [string]$Formula
    )
    $Shape.CellsU($Cell).FormulaU = $Formula
}

function Add-Box {
    param(
        [object]$Page,
        [double]$X1,
        [double]$Y1,
        [double]$X2,
        [double]$Y2,
        [string]$Text,
        [string]$Fill,
        [string]$Line,
        [double]$FontSize = 9
    )
    $shape = $Page.DrawRectangle($X1, $Y1, $X2, $Y2)
    $shape.Text = $Text
    Set-CellFormula $shape "FillForegnd" $Fill
    Set-CellFormula $shape "LineColor" $Line
    Set-CellFormula $shape "LineWeight" "1.25 pt"
    Set-CellFormula $shape "Char.Size" "$FontSize pt"
    Set-CellFormula $shape "Para.HorzAlign" "1"
    Set-CellFormula $shape "VerticalAlign" "1"
    return $shape
}

function Add-Label {
    param(
        [object]$Page,
        [double]$X,
        [double]$Y,
        [double]$W,
        [double]$H,
        [string]$Text,
        [double]$FontSize = 12
    )
    $shape = $Page.DrawRectangle($X, $Y, $X + $W, $Y + $H)
    $shape.Text = $Text
    Set-CellFormula $shape "FillPattern" "0"
    Set-CellFormula $shape "LinePattern" "0"
    Set-CellFormula $shape "Char.Size" "$FontSize pt"
    Set-CellFormula $shape "Char.Style" "17"
    Set-CellFormula $shape "Para.HorzAlign" "0"
    return $shape
}

function Add-Arrow {
    param(
        [object]$Page,
        [double]$X1,
        [double]$Y1,
        [double]$X2,
        [double]$Y2,
        [string]$Text = ""
    )
    $line = $Page.DrawLine($X1, $Y1, $X2, $Y2)
    Set-CellFormula $line "LineColor" "RGB(51,65,85)"
    Set-CellFormula $line "LineWeight" "1.25 pt"
    Set-CellFormula $line "EndArrow" "4"
    if ($Text) {
        $line.Text = $Text
        Set-CellFormula $line "Char.Size" "8 pt"
    }
    return $line
}

function Add-Band {
    param(
        [object]$Page,
        [double]$X1,
        [double]$Y1,
        [double]$X2,
        [double]$Y2,
        [string]$Title
    )
    $band = $Page.DrawRectangle($X1, $Y1, $X2, $Y2)
    Set-CellFormula $band "FillForegnd" "RGB(248,250,252)"
    Set-CellFormula $band "LineColor" "RGB(203,213,225)"
    Set-CellFormula $band "LineWeight" "1 pt"
    $band.SendToBack()
    Add-Label $Page ($X1 + 0.15) ($Y2 - 0.35) 3.6 0.25 $Title 11 | Out-Null
}

$visio = Get-VisioApplication
$visio.Visible = $true

if ($visio.Documents.Count -eq 0) {
    $document = $visio.Documents.Add("")
}
else {
    $document = $visio.ActiveDocument
}

$page = $document.Pages.Add()
$page.Name = $PageName
$page.PageSheet.CellsU("PageWidth").FormulaU = "16 in"
$page.PageSheet.CellsU("PageHeight").FormulaU = "11 in"

Add-Label $page 0.35 10.35 6.5 0.35 "Bank OCR Test Platform Architecture" 18 | Out-Null
Add-Label $page 0.35 10.05 9.5 0.25 "FastAPI review APIs, OCR integration, quality checks, parsers, rule engine, synthetic data and pytest coverage" 9 | Out-Null

Add-Band $page 0.3 8.55 15.7 9.85 "Entry Points"
Add-Band $page 0.3 5.45 15.7 8.25 "Runtime Review Pipeline"
Add-Band $page 0.3 2.45 7.85 5.15 "Data and Generation"
Add-Band $page 8.15 2.45 15.7 5.15 "Scripts, Diagnostics and Tests"
Add-Band $page 0.3 0.65 15.7 2.15 "Result Semantics"

$external = "RGB(226,232,240)"
$api = "RGB(219,234,254)"
$service = "RGB(220,252,231)"
$logic = "RGB(254,243,199)"
$data = "RGB(252,231,243)"
$test = "RGB(237,233,254)"
$blue = "RGB(37,99,235)"
$green = "RGB(22,163,74)"
$orange = "RGB(217,119,6)"
$pink = "RGB(219,39,119)"
$purple = "RGB(124,58,237)"
$slate = "RGB(100,116,139)"

Add-Box $page 0.75 8.95 3.25 9.55 "Client / Swagger UI`nUpload image files" $external $slate | Out-Null
Add-Box $page 4.15 8.85 6.95 9.65 "app/main.py`nPOST /bank-card/review`nPOST /id-card/review`nReturns JSON" $api $blue | Out-Null
Add-Box $page 7.85 8.85 10.55 9.65 "Upload Handling`nsave_upload_file()`nbank-card validation`ntemporary cleanup" $api $blue | Out-Null
Add-Box $page 11.55 8.95 14.15 9.55 "reports/tmp_uploads`nShort-lived request files" $data $pink | Out-Null

Add-Arrow $page 3.25 9.25 4.15 9.25 "HTTP multipart" | Out-Null
Add-Arrow $page 6.95 9.25 7.85 9.25 | Out-Null
Add-Arrow $page 10.55 9.25 11.55 9.25 | Out-Null

Add-Box $page 0.75 6.75 3.25 7.75 "quality_check.py`ndetect_blur()`ndetect_brightness()`ndetect_glare()" $service $green | Out-Null
Add-Box $page 4.0 6.75 6.7 7.75 "ocr_service.py`nPaddleOCR lazy engine`nrecognize_text()`nNormalize OCR output" $service $green | Out-Null
Add-Box $page 7.25 7.25 10.0 8.05 "field_parser.py`nBank card fields`nnumber / date / name" $logic $orange | Out-Null
Add-Box $page 7.25 5.85 10.0 6.65 "id_card_parser.py`nSide detection`nfront / back fields" $logic $orange | Out-Null
Add-Box $page 10.85 6.75 13.45 7.75 "rule_check.py`nreview_bank_card()`nfield completeness`nquality gates" $logic $orange | Out-Null
Add-Box $page 14.1 6.95 15.25 7.55 "JSON`nreview_result`nfields" $api $blue 8 | Out-Null

Add-Arrow $page 5.55 8.85 2.0 7.75 "image_path" | Out-Null
Add-Arrow $page 5.55 8.85 5.35 7.75 "image_path" | Out-Null
Add-Arrow $page 6.7 7.25 7.25 7.65 "OCR text" | Out-Null
Add-Arrow $page 6.7 7.1 7.25 6.25 "OCR text" | Out-Null
Add-Arrow $page 3.25 7.25 10.85 7.25 "quality" | Out-Null
Add-Arrow $page 10.0 7.65 10.85 7.35 "bank fields" | Out-Null
Add-Arrow $page 10.0 6.25 10.85 7.05 "id fields + side" | Out-Null
Add-Arrow $page 13.45 7.25 14.1 7.25 | Out-Null

Add-Box $page 0.75 3.55 3.35 4.35 "data/synthetic`nSynthetic bank cards`nSynthetic ID cards" $data $pink | Out-Null
Add-Box $page 4.25 3.35 7.1 4.55 "data/processed`nbank_card/normal`nblur / glare / occlusion`nrotate / dark / bright" $data $pink | Out-Null
Add-Box $page 0.75 2.75 3.35 3.15 "data/annotations`nlabels.json" $data $pink 8 | Out-Null
Add-Arrow $page 3.35 3.95 4.25 3.95 "processed fixtures" | Out-Null

Add-Box $page 8.6 3.35 11.5 4.55 "scripts/`ngenerate_synthetic_*.py`naugment_*.py`nanalyze_quality_distribution.py" $test $purple | Out-Null
Add-Box $page 12.1 3.35 15.0 4.55 "tests/`nAPI contract tests`nparser tests`nquality / rule / OCR tests" $test $purple | Out-Null
Add-Box $page 10.35 2.75 13.25 3.15 "reports/test-artifacts`nquality_distribution.csv" $data $pink 8 | Out-Null
Add-Arrow $page 11.5 3.95 12.1 3.95 "pytest" | Out-Null
Add-Arrow $page 9.85 3.35 10.85 3.15 "reports" | Out-Null

Add-Box $page 0.75 1.1 4.2 1.65 "pass`nrequired fields present + quality pass" $service $green 8 | Out-Null
Add-Box $page 5.0 1.1 9.6 1.65 "review`nmissing fields, bad quality, unknown side, manual check" $logic $orange 8 | Out-Null
Add-Box $page 10.4 1.1 14.0 1.65 "reject`ninvalid bank-card number format" $api $blue 8 | Out-Null

$visio.ActiveWindow.Page = $page
$visio.ActiveWindow.ViewFit = 1

Write-Output "Created Visio page '$PageName' in document '$($document.Name)'."
