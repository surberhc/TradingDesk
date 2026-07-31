Set shell = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  If i > 0 Then cmd = cmd & " "
  cmd = cmd & Chr(34) & WScript.Arguments(i) & Chr(34)
Next
shell.Run cmd, 0, False
