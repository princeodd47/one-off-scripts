#Requires AutoHotkey v2.0
#SingleInstance Force

DelayMs := 7000  ; time between pressing F1 and keys actually going down

F1:: {
    ToolTip "Starting in " . Round(DelayMs / 1000) . "s... go launch/focus your game"
    Sleep DelayMs
    ToolTip
    Send "{Space Down}"
    Click "Left Down"
}

p:: {
    Send "{Space Up}"
    Click "Left Up"
    ToolTip
}
