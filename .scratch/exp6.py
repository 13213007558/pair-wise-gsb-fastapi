from fastapi import Form

fi = Form(strict=True, annotation=int)
print("metadata:", fi.metadata)
print("attributes dict:", fi._attributes)
fi2 = Form(annotation=int)
print("no-strict metadata:", fi2.metadata)
print("no-strict attributes:", fi2._attributes)
