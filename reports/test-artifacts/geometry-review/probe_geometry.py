from pathlib import Path
import cv2
import numpy as np


def geometry(path):
    im = cv2.imread(str(path))
    h, w = im.shape[:2]
    if max(h, w) > 1000:
        im = cv2.resize(im, None, fx=1000/max(h,w), fy=1000/max(h,w))
    h, w = im.shape[:2]
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    detail = cv2.max(cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel), cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel))
    mask = (detail > 25).astype('uint8')*255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3,int(w*.025)),3)))
    contours,_ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles=[]
    for c in contours:
        (cx,cy),(rw,rh),angle=cv2.minAreaRect(c)
        long,short=max(rw,rh),min(rw,rh)
        if long < w*.1 or short < 4 or short > h*.13 or long/max(short,1) < 2.5: continue
        if rw < rh: angle += 90
        angle = (angle+90)%180-90
        if abs(angle) < 30: angles.append((round(angle,2),round(long),round(cx),round(cy)))
    hsv=cv2.cvtColor(im,cv2.COLOR_BGR2HSV)
    local_range=np.max(cv2.dilate(im,np.ones((3,3),dtype='uint8')).astype('int16')-cv2.erode(im,np.ones((3,3),dtype='uint8')),axis=2)
    mask=((hsv[:,:,2] < 70)&(hsv[:,:,1] < 90)&(local_range<5)).astype('uint8')
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),dtype='uint8'))
    count, labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
    patches=[]
    for idx in range(1,count):
        x,y,bw,bh,area=stats[idx]
        if area/(w*h)<.008 or area/(bw*bh)<.88 or bw < w*.1 or bh < h*.035 or bw > w*.85 or bh > h*.6: continue
        if x<2 or y<2 or x+bw>w-2 or y+bh>h-2:continue
        pixels=gray[labels==idx]
        patches.append((int(x),int(y),int(bw),int(bh),round(area/(w*h),3),round(float(pixels.std()),2)))
    return angles, patches


if __name__=='__main__':
    for kind in ['normal','occlusion','rotate']:
        for i in [1,2,3,27,99]:
            p=Path('data/processed/bank_card')/kind/f'bank_card_{i:04}.png'
            print(p,geometry(p))
