@echo off
REM 打包 ShortDramaPackTool.exe（需先 pip install pyinstaller）
pyinstaller --onefile --windowed --name ShortDramaPackTool --icon app.ico --clean --noconfirm short_drama_pack_tool.py
echo.
echo Done. Output: dist\ShortDramaPackTool.exe
pause
